//! Python Discord SDK I/O driven by the native provider policy.
use crate::{convert, host};
use agentchat_core::Attachment;
use agentchat_providers::discord as policy;
use pyo3::{
    exceptions::{PyAttributeError, PyModuleNotFoundError, PyRuntimeError, PyValueError},
    prelude::*,
    types::{PyDict, PyTuple, PyType},
};
type Output = PyResult<Py<PyAny>>;
fn sdk(py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
    py.import("discord").map_err(|e| {
        if e.is_instance_of::<PyModuleNotFoundError>(py) {
            PyModuleNotFoundError::new_err(
                "Install Discord support with: pip install 'agentchat[discord]'",
            )
        } else {
            e
        }
    })
}
#[pyfunction]
pub fn load_discord(py: Python<'_>) -> Output {
    sdk(py)?;
    Ok(py.get_type::<Discord>().into_any().unbind())
}
#[pyfunction]
fn discord_getattr(py: Python<'_>, name: &str) -> Output {
    if name == "Discord" {
        Ok(py
            .import("agentchat.channels.discord.adapter")?
            .getattr("Discord")?
            .unbind())
    } else {
        Err(PyAttributeError::new_err(format!(
            "module 'agentchat.channels.discord' has no attribute {name:?}"
        )))
    }
}
#[pyfunction]
fn discord_dir() -> Vec<&'static str> {
    vec!["Discord"]
}
fn event_from_python(event: &Bound<'_, PyAny>) -> PyResult<policy::Event> {
    let py = event.py();
    let module = sdk(py)?;
    let author = event.getattr("author")?;
    let channel = event.getattr("channel")?;
    let guild = event.getattr("guild")?;
    let reference = event.getattr("reference")?;
    let replied_author = if !reference.is_none() {
        let resolved = reference.getattr("resolved")?;
        if resolved.is_instance(&module.getattr("Message")?)? {
            Some(resolved.getattr("author")?.getattr("id")?.extract()?)
        } else {
            None
        }
    } else {
        None
    };
    let attachments = event
        .getattr("attachments")?
        .try_iter()?
        .map(|item| {
            let a = item?;
            Ok(Attachment {
                id: a.getattr("id")?.str()?.to_string(),
                name: a.getattr("filename")?.extract()?,
                media_type: a.getattr("content_type")?.extract()?,
                size: a.getattr("size")?.extract()?,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(policy::Event {
        id: event.getattr("id")?.extract()?,
        channel_id: channel.getattr("id")?.extract()?,
        guild_id: if guild.is_none() {
            None
        } else {
            Some(guild.getattr("id")?.extract()?)
        },
        author_id: author.getattr("id")?.extract()?,
        display_name: author
            .getattr("display_name")
            .ok()
            .and_then(|v| v.extract().ok())
            .flatten(),
        author_bot: author.getattr("bot")?.is_truthy()?,
        webhook: event.getattr("webhook_id")?.is_truthy()?,
        system: event.call_method0("is_system")?.is_truthy()?,
        mentions: event
            .getattr("mentions")?
            .try_iter()?
            .map(|v| v?.getattr("id")?.extract())
            .collect::<PyResult<Vec<_>>>()?,
        replied_author,
        is_thread: channel.is_instance(&module.getattr("Thread")?)?,
        content: event.getattr("content")?.extract()?,
        attachments,
    })
}
#[pyfunction]
#[pyo3(signature=(event,*,bot_id))]
fn discord_normalize(py: Python<'_>, event: &Bound<'_, PyAny>, bot_id: u64) -> Output {
    match policy::normalize(event_from_python(event)?, bot_id) {
        Some(message) => convert::to_message(py, &message),
        None => Ok(py.None()),
    }
}
#[pyfunction]
fn discord_text_payload(content: &Bound<'_, PyAny>) -> PyResult<String> {
    let text = content
        .extract::<String>()
        .map_err(|_| PyValueError::new_err("Discord replies require nonempty text"))?;
    policy::text_payload(&text).map_err(PyValueError::new_err)?;
    Ok(text)
}
#[pyclass(
    name = "_DiscordBase",
    dict,
    weakref,
    subclass,
    module = "agentchat._native"
)]
pub struct Discord {
    // A tracked Python object may be visited while CPython is allocating its
    // dictionary, before PyO3 installs the Rust payload. The zeroed state is valid.
    inner: Option<Box<DiscordState>>,
}
pub struct DiscordState {
    _client: Py<PyAny>,
    _sender: Py<PyAny>,
    _delivery: Py<PyAny>,
    _close_lock: Py<PyAny>,
    _closed: bool,
    _token: String,
}
impl std::ops::Deref for Discord {
    type Target = DiscordState;
    fn deref(&self) -> &Self::Target {
        self.inner.as_deref().expect("initialized native provider")
    }
}
impl std::ops::DerefMut for Discord {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("initialized native provider")
    }
}

#[pymethods]
impl Discord {
    #[getter(_client)]
    fn get_client(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self
            .inner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        Ok(state._client.clone_ref(py))
    }
    #[setter(_client)]
    fn set_client(&mut self, value: Py<PyAny>) -> PyResult<()> {
        let state = self
            .inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        state._client = value;
        Ok(())
    }
    #[getter(_sender)]
    fn get_sender(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self
            .inner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        Ok(state._sender.clone_ref(py))
    }
    #[setter(_sender)]
    fn set_sender(&mut self, value: Py<PyAny>) -> PyResult<()> {
        let state = self
            .inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        state._sender = value;
        Ok(())
    }
    #[getter(_delivery)]
    fn get_delivery(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self
            .inner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        Ok(state._delivery.clone_ref(py))
    }
    #[setter(_delivery)]
    fn set_delivery(&mut self, value: Py<PyAny>) -> PyResult<()> {
        let state = self
            .inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        state._delivery = value;
        Ok(())
    }
    #[getter(_close_lock)]
    fn get_close_lock(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self
            .inner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        Ok(state._close_lock.clone_ref(py))
    }
    #[setter(_close_lock)]
    fn set_close_lock(&mut self, value: Py<PyAny>) -> PyResult<()> {
        let state = self
            .inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        state._close_lock = value;
        Ok(())
    }
    #[getter(_closed)]
    fn get_closed(&self) -> PyResult<bool> {
        let state = self
            .inner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        Ok(state._closed)
    }
    #[setter(_closed)]
    fn set_closed(&mut self, value: bool) -> PyResult<()> {
        let state = self
            .inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("Native provider has been cleared"))?;
        state._closed = value;
        Ok(())
    }
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Some(state) = self.inner.as_ref() {
            visit.call(&state._client)?;
            visit.call(&state._sender)?;
            visit.call(&state._delivery)?;
            visit.call(&state._close_lock)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.inner = None;
    }
    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self { inner: None }
    }
    #[pyo3(signature=(*,bot_token,client=None,send_client=None,max_pending_events=64,shutdown_timeout=20.0))]
    fn initialize(
        slf: Py<Self>,
        py: Python<'_>,
        bot_token: String,
        client: Option<Py<PyAny>>,
        send_client: Option<Py<PyAny>>,
        max_pending_events: usize,
        shutdown_timeout: f64,
    ) -> PyResult<()> {
        if bot_token.trim().is_empty() {
            return Err(PyValueError::new_err("Discord bot_token is required"));
        }
        let module = sdk(py)?;
        let client = match client {
            Some(v) => v,
            None => {
                let intents = module.getattr("Intents")?.call_method0("none")?;
                for attr in ["guilds", "guild_messages", "dm_messages"] {
                    intents.setattr(attr, true)?;
                }
                let args = PyDict::new(py);
                args.set_item("intents", intents)?;
                args.set_item(
                    "allowed_mentions",
                    module.getattr("AllowedMentions")?.call_method0("none")?,
                )?;
                module.getattr("Client")?.call((), Some(&args))?.unbind()
            }
        };
        let sender = match send_client {
            Some(v) => v,
            None => Py::new(py, DiscordRESTClient::build(py, bot_token.clone()))?.into_any(),
        };
        let delivery = py
            .import("agentchat._delivery")?
            .getattr("DeliveryTasks")?
            .call1((max_pending_events, shutdown_timeout))?
            .unbind();
        slf.borrow_mut(py).inner = Some(Box::new(DiscordState {
            _client: client,
            _sender: sender,
            _delivery: delivery,
            _close_lock: py.import("asyncio")?.getattr("Lock")?.call0()?.unbind(),
            _closed: false,
            _token: bot_token,
        }));
        let callback = host::callback(py, slf.bind(py).getattr("_on_message")?.unbind())?;
        slf.borrow(py)
            ._client
            .bind(py)
            .setattr("on_message", callback)?;
        Ok(())
    }
    #[classattr]
    fn name() -> &'static str {
        "discord"
    }
    #[classmethod]
    fn from_env(cls: &Bound<'_, PyType>) -> Output {
        let py = cls.py();
        let token = py
            .import("os")?
            .call_method1("getenv", ("DISCORD_BOT_TOKEN",))?;
        if !token.is_truthy()? {
            return Err(PyRuntimeError::new_err("DISCORD_BOT_TOKEN is required"));
        }
        let kw = PyDict::new(py);
        kw.set_item("bot_token", token)?;
        Ok(cls.call((), Some(&kw))?.unbind())
    }
    fn bind(&self, py: Python<'_>, receiver: Py<PyAny>) -> PyResult<()> {
        self._delivery.bind(py).setattr("receiver", receiver)
    }
    fn run(slf: Py<Self>, py: Python<'_>) -> Output {
        host::coroutine(py, async move {
            Python::attach(|py| {
                slf.borrow(py)
                    ._delivery
                    .bind(py)
                    .call_method0("check_bound")
                    .map(|_| ())
            })?;
            let result = async {
                let start = Python::attach(|py| {
                    let this = slf.borrow(py);
                    Ok::<_, PyErr>(
                        this._client
                            .bind(py)
                            .call_method1("start", (&this._token,))?
                            .unbind(),
                    )
                })?;
                host::await_py(start).await
            }
            .await;
            let close = Python::attach(|py| slf.bind(py).call_method0("close").map(Bound::unbind))?;
            host::await_py(close).await?;
            result.map(|_| Python::attach(|py| py.None()))
        })
    }
    fn close(slf: Py<Self>, py: Python<'_>) -> Output {
        host::coroutine(py, async move {
            let lock = Python::attach(|py| slf.borrow(py)._close_lock.clone_ref(py));
            host::await_py(Python::attach(|py| {
                lock.bind(py).call_method0("__aenter__").map(Bound::unbind)
            })?)
            .await?;
            let result = async {
                let resources = Python::attach(|py| {
                    let mut this = slf.borrow_mut(py);
                    if this._closed {
                        return Ok(None);
                    }
                    this._closed = true;
                    this._delivery.bind(py).setattr("closed", true)?;
                    Ok::<_, PyErr>(Some([
                        this._delivery.clone_ref(py),
                        this._client.clone_ref(py),
                        this._sender.clone_ref(py),
                    ]))
                })?;
                let mut result = Ok(());
                if let Some(resources) = resources {
                    for resource in resources {
                        let call = Python::attach(|py| {
                            resource.bind(py).call_method0("close").map(Bound::unbind)
                        });
                        let next = match call {
                            Ok(v) => host::await_py(v).await.map(|_| ()),
                            Err(e) => Err(e),
                        };
                        if next.is_err() {
                            result = next;
                        }
                    }
                }
                result
            }
            .await;
            host::await_py(Python::attach(|py| {
                lock.bind(py)
                    .call_method1("__aexit__", (py.None(), py.None(), py.None()))
                    .map(Bound::unbind)
            })?)
            .await?;
            result?;
            Ok(Python::attach(|py| py.None()))
        })
    }
    fn _on_message(slf: Py<Self>, py: Python<'_>, event: Py<PyAny>) -> Output {
        host::coroutine(py, async move {
            Python::attach(|py| {
                let this = slf.borrow(py);
                let user = this._client.bind(py).getattr("user")?;
                if user.is_none() || this._delivery.bind(py).getattr("closed")?.is_truthy()? {
                    return Ok(py.None());
                }
                if let Some(message) = policy::normalize(
                    event_from_python(event.bind(py))?,
                    user.getattr("id")?.extract()?,
                ) {
                    let message = convert::to_message(py, &message)?;
                    this._delivery
                        .bind(py)
                        .call_method1("submit", (slf.clone_ref(py), message))?;
                }
                Ok(py.None())
            })
        })
    }
    fn reply(slf: Py<Self>, py: Python<'_>, source: Py<PyAny>, content: Py<PyAny>) -> Output {
        host::coroutine(py, async move {
            let source_host = Python::attach(|py| source.clone_ref(py));
            let (source, content, destination, display_name, call) = Python::attach(|py| {
                let content = discord_text_payload(content.bind(py))?;
                let source = convert::from_message(source.bind(py))?;
                let this = slf.borrow(py);
                let bot = this._client.bind(py).getattr("user")?;
                if bot.is_none() {
                    return Err(PyValueError::new_err(
                        "Source does not belong to this Discord adapter",
                    ));
                }
                let destination =
                    policy::Destination::from_source(&source, bot.getattr("id")?.extract()?)
                        .map_err(PyValueError::new_err)?;
                if this
                    ._client
                    .bind(py)
                    .call_method0("is_closed")?
                    .is_truthy()?
                {
                    return Err(PyRuntimeError::new_err("Discord connection is closed"));
                }
                this._delivery.bind(py).call_method0("check_reply")?;
                let display_name = bot.getattr("display_name")?.extract::<Option<String>>()?;
                let call = this
                    ._sender
                    .bind(py)
                    .call_method1(
                        "send_text",
                        (
                            destination.channel_id,
                            convert::from_json(py, &destination.payload(&content))?,
                        ),
                    )?
                    .unbind();
                Ok((source, content, destination, display_name, call))
            })?;
            let receipt = host::await_py(call).await?;
            Python::attach(|py| {
                let receipt = receipt.bind(py).extract::<u64>().map_err(|_| {
                    PyRuntimeError::new_err("Discord did not return a message receipt")
                })?;
                let message = destination
                    .confirmed_reply(&source, content, receipt, display_name)
                    .map_err(PyRuntimeError::new_err)?;
                super::transport::receipt_with_metadata(&source_host, &message)
            })
        })
    }
}
#[pyclass(
    dict,
    weakref,
    subclass,
    module = "agentchat.channels.discord.transport"
)]
pub struct DiscordRESTClient {
    inner: Option<Box<DiscordRESTClientState>>,
}
pub struct DiscordRESTClientState {
    _token: String,
    _session: Py<PyAny>,
    _closed: bool,
}
impl std::ops::Deref for DiscordRESTClient {
    type Target = DiscordRESTClientState;
    fn deref(&self) -> &Self::Target {
        self.inner
            .as_deref()
            .expect("native object has been cleared")
    }
}
impl std::ops::DerefMut for DiscordRESTClient {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.inner
            .as_deref_mut()
            .expect("native object has been cleared")
    }
}
#[pymethods]
impl DiscordRESTClient {
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Some(state) = &self.inner {
            visit.call(&state._session)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.inner = None;
    }
    #[getter(_session)]
    fn session(&self, py: Python<'_>) -> Py<PyAny> {
        self._session.clone_ref(py)
    }
    #[setter(_session)]
    fn set_session(&mut self, value: Py<PyAny>) {
        self._session = value;
    }
    #[getter(_closed)]
    fn closed(&self) -> bool {
        self._closed
    }
    #[setter(_closed)]
    fn set_closed(&mut self, value: bool) {
        self._closed = value;
    }

    #[new]
    #[pyo3(signature=(*_args, **_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self { inner: None }
    }
    fn initialize(&mut self, py: Python<'_>, bot_token: String) {
        *self = Self::build(py, bot_token);
    }
    fn send_text(slf: Py<Self>, py: Python<'_>, channel_id: u64, payload: Py<PyAny>) -> Output {
        host::coroutine(py, async move {
            let result = async {
                let context = Python::attach(|py| {
                    let mut this = slf.borrow_mut(py);
                    if this._closed {
                        return Err(PyRuntimeError::new_err("Discord send client is closed"));
                    }
                    if this._session.is_none(py) {
                        let aio = py.import("aiohttp")?;
                        let timeout_args = PyDict::new(py);
                        timeout_args.set_item("total", 30)?;
                        let args = PyDict::new(py);
                        args.set_item(
                            "timeout",
                            aio.getattr("ClientTimeout")?
                                .call((), Some(&timeout_args))?,
                        )?;
                        this._session = aio
                            .getattr("ClientSession")?
                            .call((), Some(&args))?
                            .unbind();
                    }
                    let root = py
                        .import("agentchat.channels.discord.transport")?
                        .getattr("API_ROOT")?
                        .extract::<String>()?;
                    let args = PyDict::new(py);
                    let headers = PyDict::new(py);
                    headers.set_item("Authorization", format!("Bot {}", this._token))?;
                    args.set_item("headers", headers)?;
                    args.set_item("json", payload)?;
                    args.set_item("allow_redirects", false)?;
                    Ok(this
                        ._session
                        .bind(py)
                        .call_method(
                            "post",
                            (format!("{root}/channels/{channel_id}/messages"),),
                            Some(&args),
                        )?
                        .unbind())
                })?;
                let response = host::await_py(Python::attach(|py| {
                    context
                        .bind(py)
                        .call_method0("__aenter__")
                        .map(Bound::unbind)
                })?)
                .await?;
                let result = async {
                    let call = Python::attach(|py| {
                        let status = response.bind(py).getattr("status")?.extract::<u16>()?;
                        if !(200..300).contains(&status) {
                            return Err(PyRuntimeError::new_err(format!(
                                "Discord send failed (HTTP {status})"
                            )));
                        }
                        Ok(response.bind(py).call_method0("json")?.unbind())
                    })?;
                    let data = host::await_py(call).await?;
                    Python::attach(|py| {
                        let id = policy::receipt(&convert::to_json(data.bind(py))?)
                            .map_err(PyRuntimeError::new_err)?;
                        Ok(id.into_pyobject(py)?.into_any().unbind())
                    })
                }
                .await;
                host::await_py(Python::attach(|py| {
                    context
                        .bind(py)
                        .call_method1("__aexit__", (py.None(), py.None(), py.None()))
                        .map(Bound::unbind)
                })?)
                .await?;
                result
            }
            .await;
            result.map_err(|e: PyErr| {
                Python::attach(|py| {
                    let network = py
                        .import("aiohttp")
                        .and_then(|m| m.getattr("ClientError"))
                        .ok()
                        .is_some_and(|t| e.is_instance(py, &t));
                    if network
                        || e.is_instance_of::<pyo3::exceptions::PyTimeoutError>(py)
                        || e.is_instance_of::<PyValueError>(py)
                    {
                        PyRuntimeError::new_err("Discord request failed; delivery may be uncertain")
                    } else {
                        e
                    }
                })
            })
        })
    }
    fn close(slf: Py<Self>, py: Python<'_>) -> Output {
        host::coroutine(py, async move {
            let call = Python::attach(|py| {
                let mut this = slf.borrow_mut(py);
                this._closed = true;
                if this._session.is_none(py) {
                    Ok(None)
                } else {
                    Ok::<_, PyErr>(Some(this._session.bind(py).call_method0("close")?.unbind()))
                }
            })?;
            if let Some(call) = call {
                host::await_py(call).await?;
            }
            Ok(Python::attach(|py| py.None()))
        })
    }
}
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Discord>()?;
    host::register_initialized_class::<DiscordRESTClient>(m)?;
    m.add_function(wrap_pyfunction!(load_discord, m)?)?;
    m.add_function(wrap_pyfunction!(discord_getattr, m)?)?;
    m.add_function(wrap_pyfunction!(discord_dir, m)?)?;
    m.add_function(wrap_pyfunction!(discord_normalize, m)?)?;
    m.add_function(wrap_pyfunction!(discord_text_payload, m)?)?;
    Ok(())
}

impl DiscordRESTClient {
    fn build(py: Python<'_>, bot_token: String) -> Self {
        Self {
            inner: Some(Box::new(DiscordRESTClientState {
                _token: bot_token,
                _session: py.None(),
                _closed: false,
            })),
        }
    }
}
