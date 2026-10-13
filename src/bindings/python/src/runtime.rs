use crate::host::{await_py, coroutine};
use agentchat_core::{HistoryLimit, Turn, TurnEffect, TurnResult};
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError, PyValueError},
    prelude::*,
    types::{PyDict, PyTuple},
    PyTraverseError, PyVisit,
};
type Object = Py<PyAny>;
pub fn copy(value: &Object) -> Object {
    Python::attach(|py| value.clone_ref(py))
}
pub fn none() -> Object {
    Python::attach(|py| py.None())
}
pub async fn call(
    target: Object,
    method: &str,
    args: Vec<Object>,
    kwargs: Vec<(&str, Object)>,
) -> PyResult<Object> {
    let pending = Python::attach(|py| -> PyResult<_> {
        let kw = PyDict::new(py);
        for (key, value) in kwargs {
            kw.set_item(key, value)?;
        }
        Ok(target
            .bind(py)
            .call_method(method, PyTuple::new(py, args)?, Some(&kw))?
            .unbind())
    })?;
    await_py(pending).await
}
fn capability(py: Python<'_>, channel: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
    channel.is_instance(&py.import("agentchat.channels.base")?.getattr(name)?)
}
pub async fn exit_scope(scope: Object, error: Option<&PyErr>) -> PyResult<bool> {
    let args = Python::attach(|py| match error {
        Some(error) => vec![
            error.get_type(py).into_any().unbind(),
            error.value(py).clone().into_any().unbind(),
            error
                .traceback(py)
                .map(|v| v.into_any().unbind())
                .unwrap_or_else(|| py.None()),
        ],
        None => vec![py.None(), py.None(), py.None()],
    });
    let result = call(scope, "__aexit__", args, vec![]).await?;
    Python::attach(|py| result.bind(py).is_truthy())
}
#[pyclass(
    name = "_AgentChatBase",
    dict,
    subclass,
    weakref,
    module = "agentchat._native"
)]
pub struct AgentChat {
    channels: Option<Object>,
    state: Option<Object>,
    handler: Option<Object>,
}
#[pymethods]
impl AgentChat {
    #[new]
    #[pyo3(signature=(*_args,**_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self {
            channels: None,
            state: None,
            handler: None,
        }
    }
    #[pyo3(signature=(*,channels,state=None,backend=None))]
    fn initialize(
        slf: Py<Self>,
        py: Python<'_>,
        channels: &Bound<'_, PyAny>,
        state: Option<Object>,
        backend: Option<&str>,
    ) -> PyResult<()> {
        crate::state::backend(backend)?;
        let channels = PyTuple::new(py, channels.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
        if channels.is_empty() {
            return Err(PyValueError::new_err(
                "AgentChat requires at least one channel",
            ));
        }
        let state = match state {
            Some(state) if state.bind(py).is_truthy()? => state,
            _ => py.get_type::<crate::state::MemoryState>().call0()?.unbind(),
        };
        {
            let mut this = slf.borrow_mut(py);
            this.channels = Some(channels.clone().unbind().into_any());
            this.state = Some(state);
            this.handler = None;
        }
        let receiver = slf.bind(py).getattr("_receive")?;
        for channel in channels.iter() {
            channel.call_method1("bind", (&receiver,))?;
        }
        Ok(())
    }
    #[getter]
    fn channels(&self, py: Python<'_>) -> PyResult<Object> {
        self.channels
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("AgentChat is uninitialized"))
    }
    #[setter(channels)]
    fn set_channels(&mut self, channels: Object) {
        self.channels = Some(channels);
    }
    #[getter]
    fn state(&self, py: Python<'_>) -> PyResult<Object> {
        self.state
            .as_ref()
            .map(|v| v.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("AgentChat is uninitialized"))
    }
    #[setter(state)]
    fn set_state(&mut self, state: Object) {
        self.state = Some(state);
    }
    #[getter]
    fn backend(&self) -> &'static str {
        "rust"
    }
    #[getter(_handler)]
    fn handler(&self, py: Python<'_>) -> Object {
        self.handler
            .as_ref()
            .map(|v| v.clone_ref(py))
            .unwrap_or_else(|| py.None())
    }
    fn on_message(&mut self, py: Python<'_>, handler: Object) -> PyResult<Object> {
        if self.handler.is_some() {
            return Err(PyRuntimeError::new_err(
                "AgentChat supports one message handler",
            ));
        }
        self.handler = Some(handler.clone_ref(py));
        Ok(handler)
    }
    fn run(slf: Py<Self>, py: Python<'_>) -> PyResult<Object> {
        coroutine(py, async move {
            let pending = Python::attach(|py| -> PyResult<_> {
                let this = slf.borrow(py);
                if this.handler.is_none() {
                    return Err(PyRuntimeError::new_err(
                        "Register a message handler with @app.on_message",
                    ));
                }
                let channels = this.channels(py)?;
                drop(this);
                let futures: Vec<_> = channels
                    .bind(py)
                    .try_iter()?
                    .map(|channel| channel?.call_method0("run"))
                    .collect::<PyResult<_>>()?;
                Ok(py
                    .import("asyncio")?
                    .call_method1("gather", PyTuple::new(py, futures)?)?
                    .unbind())
            })?;
            await_py(pending).await?;
            Ok(none())
        })
    }
    fn close(slf: Py<Self>, py: Python<'_>) -> PyResult<Object> {
        coroutine(py, async move {
            let pending = Python::attach(|py| -> PyResult<_> {
                let channels = slf.borrow(py).channels(py)?;
                let futures: Vec<_> = channels
                    .bind(py)
                    .try_iter()?
                    .map(|channel| channel?.call_method0("close"))
                    .collect::<PyResult<_>>()?;
                Ok(py
                    .import("asyncio")?
                    .call_method1("gather", PyTuple::new(py, futures)?)?
                    .unbind())
            })?;
            await_py(pending).await?;
            Ok(none())
        })
    }
    fn reply(
        slf: Py<Self>,
        py: Python<'_>,
        channel: Object,
        source: Object,
        content: Object,
    ) -> PyResult<Object> {
        send_reply(py, slf.into_any(), channel, source, content, false)
    }
    fn reply_rich(
        slf: Py<Self>,
        py: Python<'_>,
        channel: Object,
        source: Object,
        content: Object,
    ) -> PyResult<Object> {
        send_reply(py, slf.into_any(), channel, source, content, true)
    }
    fn upload_files(
        &self,
        py: Python<'_>,
        channel: Object,
        source: Object,
        files: Object,
    ) -> PyResult<Object> {
        coroutine(py, async move {
            Python::attach(|py| -> PyResult<()> {
                if !capability(py, channel.bind(py), "FileUploadChannel")? {
                    return Err(PyTypeError::new_err(
                        "This channel does not support file uploads",
                    ));
                }
                Ok(())
            })?;
            call(channel, "upload_files", vec![source, files], vec![]).await
        })
    }
    fn set_status(
        &self,
        py: Python<'_>,
        channel: Object,
        source: Object,
        status: Object,
    ) -> PyResult<Object> {
        coroutine(py, async move {
            if !Python::attach(|py| capability(py, channel.bind(py), "StatusChannel"))? {
                return Python::attach(|py| {
                    Ok(false.into_pyobject(py)?.to_owned().unbind().into_any())
                });
            }
            call(channel, "set_status", vec![source, status], vec![]).await
        })
    }
    #[pyo3(signature=(channel,source,attachment,*,max_bytes=10*1024*1024))]
    fn download_attachment(
        &self,
        py: Python<'_>,
        channel: Object,
        source: Object,
        attachment: Object,
        max_bytes: usize,
    ) -> PyResult<Object> {
        coroutine(py, async move {
            Python::attach(|py| -> PyResult<()> {
                if !capability(py, channel.bind(py), "FileDownloadChannel")? {
                    return Err(PyTypeError::new_err(
                        "This channel does not support attachment downloads",
                    ));
                }
                if !source
                    .bind(py)
                    .getattr("attachments")?
                    .contains(attachment.bind(py))?
                {
                    return Err(PyValueError::new_err(
                        "Attachment does not belong to the source message",
                    ));
                }
                Ok(())
            })?;
            let max = Python::attach(|py| -> PyResult<_> {
                Ok(max_bytes.into_pyobject(py)?.unbind().into_any())
            })?;
            call(
                channel,
                "download_attachment",
                vec![source, attachment],
                vec![("max_bytes", max)],
            )
            .await
        })
    }
    #[pyo3(signature=(channel,source,message,*,origin="web"))]
    fn mirror(
        slf: Py<Self>,
        py: Python<'_>,
        channel: Object,
        source: Object,
        message: Object,
        origin: &str,
    ) -> PyResult<Object> {
        let origin = origin.to_owned();
        coroutine(py, async move {
            Python::attach(|py| -> PyResult<()> {
                if message.bind(py).getattr("role")?.extract::<String>()? != "user" {
                    return Err(PyValueError::new_err("Only user inputs can be mirrored"));
                }
                if !capability(py, channel.bind(py), "MirroringChannel")? {
                    return Err(PyTypeError::new_err(
                        "This channel does not support mirroring",
                    ));
                }
                Ok(())
            })?;
            let origin = Python::attach(|py| -> PyResult<_> {
                Ok(origin.into_pyobject(py)?.unbind().into_any())
            })?;
            let response = call(
                channel,
                "mirror",
                vec![source, message],
                vec![("origin", origin)],
            )
            .await?;
            let state = Python::attach(|py| slf.borrow(py).state(py))?;
            call(state, "append", vec![copy(&response)], vec![]).await?;
            Ok(response)
        })
    }
    #[pyo3(signature=(channel,source,status="is working…",*,refresh_interval=60.0))]
    fn working(
        slf: Py<Self>,
        py: Python<'_>,
        channel: Object,
        source: Object,
        status: &str,
        refresh_interval: f64,
    ) -> PyResult<Object> {
        let setter = py.import("functools")?.getattr("partial")?.call1((
            slf.bind(py).getattr("set_status")?,
            channel,
            source,
        ))?;
        let kw = PyDict::new(py);
        kw.set_item("refresh_interval", refresh_interval)?;
        Ok(py
            .get_type::<crate::activity::WorkingStatus>()
            .call((setter, status), Some(&kw))?
            .unbind())
    }
    fn _receive(
        slf: Py<Self>,
        py: Python<'_>,
        channel: Object,
        message: Object,
    ) -> PyResult<Object> {
        let app = slf.into_any();
        coroutine(py, receive(app, channel, message))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.channels)?;
        visit.call(&self.state)?;
        visit.call(&self.handler)
    }
    fn __clear__(&mut self) {
        self.channels = None;
        self.state = None;
        self.handler = None;
    }
}
fn send_reply(
    py: Python<'_>,
    app: Object,
    channel: Object,
    source: Object,
    content: Object,
    rich: bool,
) -> PyResult<Object> {
    coroutine(py, async move {
        if rich {
            Python::attach(|py| -> PyResult<()> {
                if !capability(py, channel.bind(py), "RichReplyChannel")? {
                    return Err(PyTypeError::new_err(
                        "This channel does not support rich replies",
                    ));
                }
                Ok(())
            })?;
        }
        let response = call(
            channel,
            if rich { "reply_rich" } else { "reply" },
            vec![source, content],
            vec![],
        )
        .await?;
        let state = Python::attach(|py| app.bind(py).getattr("state").map(Bound::unbind))?;
        call(state, "append", vec![copy(&response)], vec![]).await?;
        Ok(response)
    })
}
async fn receive(app: Object, channel: Object, message: Object) -> PyResult<Object> {
    let (state, handler, id, conversation) = Python::attach(|py| -> PyResult<_> {
        let handler = app.bind(py).getattr("_handler")?;
        if handler.is_none() {
            return Err(PyRuntimeError::new_err(
                "Register a message handler with @app.on_message",
            ));
        }
        Ok((
            app.bind(py).getattr("state")?.unbind(),
            handler.unbind(),
            message.bind(py).getattr("id")?.unbind(),
            message.bind(py).getattr("conversation_id")?.unbind(),
        ))
    })?;
    let mut turn = Turn::new();
    let mut scope = None;
    let result: PyResult<Object> = async {
        loop {
            let result = match turn.effect() {
                TurnEffect::Claim => {
                    let claimed = call(copy(&state), "claim", vec![copy(&id)], vec![]).await?;
                    TurnResult::Claimed(Python::attach(|py| claimed.bind(py).is_truthy())?)
                }
                TurnEffect::AcquireConversation => {
                    let lock = Python::attach(|py| {
                        state
                            .bind(py)
                            .call_method1("lock", (conversation.bind(py),))
                            .map(Bound::unbind)
                    })?;
                    call(copy(&lock), "__aenter__", vec![], vec![]).await?;
                    scope = Some(lock);
                    TurnResult::ConversationAcquired
                }
                TurnEffect::AppendIncoming => {
                    call(copy(&state), "append", vec![copy(&message)], vec![]).await?;
                    TurnResult::IncomingAppended
                }
                TurnEffect::InvokeHandler => {
                    let pending = Python::attach(|py| -> PyResult<_> {
                        let context = Py::new(
                            py,
                            MessageContext::create(py, copy(&app), copy(&channel), copy(&message))?,
                        )?;
                        Ok(handler.bind(py).call1((context,))?.unbind())
                    })?;
                    let answer = await_py(pending).await?;
                    TurnResult::HandlerCompleted(Python::attach(|py| {
                        answer.bind(py).extract::<Option<String>>()
                    })?)
                }
                TurnEffect::SendReply(text) => {
                    let text = Python::attach(|py| {
                        text.clone()
                            .into_pyobject(py)
                            .map(|v| v.unbind().into_any())
                    })?;
                    call(
                        copy(&app),
                        "reply",
                        vec![copy(&channel), copy(&message), text],
                        vec![],
                    )
                    .await?;
                    TurnResult::ReplySent
                }
                TurnEffect::AppendReply => TurnResult::ReplyAppended,
                TurnEffect::Complete => break Ok(none()),
            };
            turn.resolve(result)
                .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        }
    }
    .await;
    if let Some(scope) = scope {
        if exit_scope(scope, result.as_ref().err()).await? {
            return Ok(none());
        }
    }
    result
}
#[pyclass(dict, subclass, weakref, module = "agentchat._native")]
pub struct Conversation {
    inner: Option<Box<ConversationState>>,
}
pub struct ConversationState {
    id: Object,
    app: Object,
}
impl std::ops::Deref for Conversation {
    type Target = ConversationState;
    fn deref(&self) -> &Self::Target {
        self.inner
            .as_deref()
            .expect("native object has been cleared")
    }
}
impl std::ops::DerefMut for Conversation {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("native object has been cleared")
    }
}

#[pymethods]
impl Conversation {
    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(
        _args: &Bound<'_, pyo3::types::PyTuple>,
        _kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> Self {
        Self { inner: None }
    }
    fn initialize(&mut self, app: Object, conversation_id: Object) -> PyResult<()> {
        *self = Self::build(app, conversation_id);
        Ok(())
    }
    #[pyo3(signature=(*,limit=None))]
    fn history(&self, py: Python<'_>, limit: Option<Object>) -> PyResult<Object> {
        let app = self.app.clone_ref(py);
        let id = self.id.clone_ref(py);
        coroutine(py, async move {
            let state = Python::attach(|py| app.bind(py).getattr("state").map(Bound::unbind))?;
            call(
                state,
                "history",
                vec![id],
                vec![("limit", limit.unwrap_or_else(none))],
            )
            .await
        })
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Some(state) = &self.inner else {
            return Ok(());
        };

        visit.call(&state.id)?;
        visit.call(&state.app)
    }

    fn __clear__(&mut self) {
        self.inner = None;
    }

    #[getter(id)]
    fn get_id(&self, py: Python<'_>) -> Object {
        self.id.clone_ref(py)
    }
    #[setter(id)]
    fn set_id(&mut self, value: Object) {
        self.id = value;
    }
}
#[pyclass(dict, subclass, weakref, module = "agentchat._native")]
pub struct MessageContext {
    inner: Option<Box<MessageContextState>>,
}
pub struct MessageContextState {
    message: Object,
    sender: Object,
    conversation: Object,
    app: Object,
    channel: Object,
}
impl std::ops::Deref for MessageContext {
    type Target = MessageContextState;
    fn deref(&self) -> &Self::Target {
        self.inner
            .as_deref()
            .expect("native object has been cleared")
    }
}
impl std::ops::DerefMut for MessageContext {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("native object has been cleared")
    }
}

impl MessageContext {
    fn create(py: Python<'_>, app: Object, channel: Object, message: Object) -> PyResult<Self> {
        Ok(Self {
            inner: Some(Box::new(MessageContextState {
                sender: message.bind(py).getattr("sender")?.unbind(),
                conversation: Py::new(
                    py,
                    Conversation::build(
                        copy(&app),
                        message.bind(py).getattr("conversation_id")?.unbind(),
                    ),
                )?
                .into_any(),
                message,
                app,
                channel,
            })),
        })
    }
    fn forward(
        &self,
        py: Python<'_>,
        method: &str,
        tail: Vec<Object>,
        kwargs: Vec<(&str, Object)>,
    ) -> PyResult<Object> {
        let mut args = vec![copy(&self.channel), copy(&self.message)];
        args.extend(tail);
        let kw = PyDict::new(py);
        for (key, value) in kwargs {
            kw.set_item(key, value)?;
        }
        Ok(self
            .app
            .bind(py)
            .call_method(method, PyTuple::new(py, args)?, Some(&kw))?
            .unbind())
    }
}
#[pymethods]
impl MessageContext {
    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(
        _args: &Bound<'_, pyo3::types::PyTuple>,
        _kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> Self {
        Self { inner: None }
    }
    fn initialize(
        &mut self,
        py: Python<'_>,
        app: Object,
        channel: Object,
        message: Object,
    ) -> PyResult<()> {
        *self = Self::build(py, app, channel, message)?;
        Ok(())
    }
    #[pyo3(signature=(*,limit=30))]
    fn history(&self, py: Python<'_>, limit: isize) -> PyResult<Object> {
        let channel = copy(&self.channel);
        let message = copy(&self.message);
        let conversation = self.conversation.clone_ref(py);
        coroutine(py, async move {
            let bound = HistoryLimit::new(usize::try_from(limit).unwrap_or_default())
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            let native = Python::attach(|py| -> PyResult<_> {
                Ok(!message.bind(py).getattr("thread_id")?.is_none()
                    && capability(py, channel.bind(py), "ThreadHistoryChannel")?)
            })?;
            let limit_value =
                Python::attach(|py| limit.into_pyobject(py).map(|v| v.unbind().into_any()))?;
            if native {
                let history = call(
                    channel,
                    "thread_history",
                    vec![copy(&message)],
                    vec![("limit", limit_value)],
                )
                .await?;
                Python::attach(|py| {
                    let mut messages: Vec<_> = history
                        .bind(py)
                        .try_iter()?
                        .map(|v| v.map(Bound::unbind))
                        .collect::<PyResult<_>>()?;
                    messages.push(message);
                    Ok(PyTuple::new(
                        py,
                        bound
                            .indices(messages.len())
                            .into_iter()
                            .map(|index| messages[index].bind(py)),
                    )?
                    .unbind()
                    .into_any())
                })
            } else {
                call(
                    conversation.into_any(),
                    "history",
                    vec![],
                    vec![("limit", limit_value)],
                )
                .await
            }
        })
    }
    fn reply(&self, py: Python<'_>, content: Object) -> PyResult<Object> {
        self.forward(py, "reply", vec![content], vec![])
    }
    fn reply_rich(&self, py: Python<'_>, content: Object) -> PyResult<Object> {
        self.forward(py, "reply_rich", vec![content], vec![])
    }
    fn upload_files(&self, py: Python<'_>, files: Object) -> PyResult<Object> {
        self.forward(py, "upload_files", vec![files], vec![])
    }
    fn set_status(&self, py: Python<'_>, status: Object) -> PyResult<Object> {
        self.forward(py, "set_status", vec![status], vec![])
    }
    #[pyo3(signature=(attachment,*,max_bytes=10*1024*1024))]
    fn download_attachment(
        &self,
        py: Python<'_>,
        attachment: Object,
        max_bytes: usize,
    ) -> PyResult<Object> {
        self.forward(
            py,
            "download_attachment",
            vec![attachment],
            vec![(
                "max_bytes",
                max_bytes.into_pyobject(py)?.unbind().into_any(),
            )],
        )
    }
    #[pyo3(signature=(status="is working…",*,refresh_interval=60.0))]
    fn working(&self, py: Python<'_>, status: &str, refresh_interval: f64) -> PyResult<Object> {
        self.forward(
            py,
            "working",
            vec![status.into_pyobject(py)?.unbind().into_any()],
            vec![(
                "refresh_interval",
                refresh_interval.into_pyobject(py)?.unbind().into_any(),
            )],
        )
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let Some(state) = &self.inner else {
            return Ok(());
        };

        visit.call(&state.message)?;
        visit.call(&state.sender)?;
        visit.call(&state.conversation)?;
        visit.call(&state.app)?;
        visit.call(&state.channel)
    }

    fn __clear__(&mut self) {
        self.inner = None;
    }

    #[getter(message)]
    fn get_message(&self, py: Python<'_>) -> Object {
        self.message.clone_ref(py)
    }
    #[setter(message)]
    fn set_message(&mut self, value: Object) {
        self.message = value;
    }

    #[getter(sender)]
    fn get_sender(&self, py: Python<'_>) -> Object {
        self.sender.clone_ref(py)
    }
    #[setter(sender)]
    fn set_sender(&mut self, value: Object) {
        self.sender = value;
    }

    #[getter(conversation)]
    fn get_conversation(&self, py: Python<'_>) -> Object {
        self.conversation.clone_ref(py)
    }
    #[setter(conversation)]
    fn set_conversation(&mut self, value: Object) {
        self.conversation = value;
    }
}
#[pyfunction]
#[pyo3(signature=(backend=None))]
fn resolve_backend(backend: Option<&str>) -> PyResult<&'static str> {
    crate::state::backend(backend)
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<AgentChat>()?;
    crate::host::register_initialized_class::<MessageContext>(module)?;
    crate::host::register_initialized_class::<Conversation>(module)?;
    let py = module.py();
    let attrs = PyDict::new(py);
    attrs.set_item(
        "__init__",
        py.get_type::<AgentChat>().getattr("initialize")?,
    )?;
    attrs.set_item("__module__", "agentchat._native")?;
    let class = py.import("builtins")?.getattr("type")?.call1((
        "AgentChat",
        (py.get_type::<AgentChat>(),),
        attrs,
    ))?;
    module.add("AgentChat", class)?;
    module.add_function(wrap_pyfunction!(resolve_backend, module)?)?;
    Ok(())
}

impl Conversation {
    #[allow(clippy::too_many_arguments)]
    fn build(app: Object, conversation_id: Object) -> Self {
        Self {
            inner: Some(Box::new(ConversationState {
                id: conversation_id,
                app,
            })),
        }
    }
}

impl MessageContext {
    #[allow(clippy::too_many_arguments)]
    fn build(py: Python<'_>, app: Object, channel: Object, message: Object) -> PyResult<Self> {
        Self::create(py, app, channel, message)
    }
}
