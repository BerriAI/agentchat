//! Foreign aiohttp calls. Telegram policy and error interpretation remain portable Rust.
use super::super::transport::*;
use crate::host;
use agentchat_providers::telegram as policy;
use pyo3::{
    exceptions::{PyRuntimeError, PyStopAsyncIteration, PyValueError},
    prelude::*,
    types::{PyBytes, PyDict, PyTuple},
    PyTraverseError, PyVisit,
};
use serde_json::json;

pub(super) fn api_error(description: &str, code: Option<i64>, retry: Option<i64>) -> PyErr {
    let result = (|| {
        call(
            &module("agentchat.channels.telegram.transport")?,
            "TelegramError",
            vec![j(json!(description))?],
            Some(j(json!({"error_code":code,"retry_after":retry}))?),
        )
    })();
    match result {
        Ok(e) => Python::attach(|py| PyErr::from_value(e.into_bound(py))),
        Err(e) => e,
    }
}
fn transport_failure(error: PyErr, description: &str) -> PyErr {
    let sanitize = Python::attach(|py| {
        error.is_instance_of::<pyo3::exceptions::PyTimeoutError>(py)
            || py
                .import("aiohttp")
                .and_then(|m| m.getattr("ClientError"))
                .is_ok_and(|class| error.is_instance(py, &class))
    });
    if sanitize {
        suppress_context(api_error(description, None, None))
    } else {
        error
    }
}
#[pyfunction]
#[pyo3(signature=(instance,description,*,error_code=None,retry_after=None))]
fn telegram_error_init(
    instance: &Bound<'_, PyAny>,
    description: &str,
    error_code: Option<i64>,
    retry_after: Option<i64>,
) -> PyResult<()> {
    instance.setattr("args", (description,))?;
    instance.setattr("description", description)?;
    instance.setattr("error_code", error_code)?;
    instance.setattr("retry_after", retry_after)
}
#[pyfunction]
fn telegram_error_transient(instance: &Bound<'_, PyAny>) -> PyResult<bool> {
    let code = instance.getattr("error_code")?.extract::<Option<i64>>()?;
    Ok(policy::ApiError {
        description: String::new(),
        error_code: code,
        retry_after: None,
    }
    .is_transient())
}
#[pyclass(
    module = "agentchat.channels.telegram.transport",
    subclass,
    weakref,
    dict
)]
pub(crate) struct TelegramBotClient {
    // CPython may invoke GC traversal on zero-initialized storage before __new__.
    payload: Option<Box<TelegramBotClientState>>,
}
pub(crate) struct TelegramBotClientState {
    state: Py<PyDict>,
}
impl std::ops::Deref for TelegramBotClient {
    type Target = TelegramBotClientState;
    fn deref(&self) -> &Self::Target {
        self.payload
            .as_deref()
            .expect("methods require a successfully initialized native adapter")
    }
}
#[pymethods]
impl TelegramBotClient {
    #[new]
    #[pyo3(signature=(*_args,**_kwargs))]
    fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self { payload: None }
    }
    #[pyo3(signature=(token,*,api_root="https://api.telegram.org"))]
    fn initialize(slf: Py<Self>, py: Python<'_>, token: &str, api_root: &str) -> PyResult<()> {
        let state = slf.bind(py).getattr("__dict__")?.cast_into::<PyDict>()?;
        state.set_item("_token", token)?;
        state.set_item("_api_root", api_root.trim_end_matches('/'))?;
        state.set_item("_session", py.None())?;
        state.set_item("_closed", false)?;
        slf.borrow_mut(py).payload = Some(Box::new(TelegramBotClientState {
            state: state.unbind(),
        }));
        Ok(())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(payload) = self.payload.as_deref() {
            visit.call(&payload.state)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.payload = None;
    }
    fn _open_session(slf: Py<Self>) -> PyResult<Py<PyAny>> {
        open_session(&slf.into_any())
    }
    #[getter]
    fn is_closed(&self, py: Python<'_>) -> PyResult<bool> {
        let state = self.state.bind(py);
        let session = state.get_item("_session")?.unwrap();
        Ok(state.get_item("_closed")?.unwrap().extract::<bool>()?
            && (session.is_none() || session.getattr("closed")?.extract::<bool>()?))
    }
    #[pyo3(signature=(method,payload=None,*,request_timeout=30.0))]
    fn call(
        slf: Py<Self>,
        py: Python<'_>,
        method: String,
        payload: Option<Py<PyAny>>,
        request_timeout: f64,
    ) -> PyResult<Py<PyAny>> {
        host::coroutine(py, async move {
            let payload = payload.unwrap_or_else(dict);
            let kwargs = Python::attach(|py| -> PyResult<_> {
                let kw = PyDict::new(py);
                kw.set_item("json", payload)?;
                Ok(kw.into_any().unbind())
            })?;
            post(&slf.into_any(), &method, request_timeout, kwargs).await
        })
    }
    #[pyo3(signature=(*,method,fields,file_field,filename,content))]
    fn upload(
        slf: Py<Self>,
        py: Python<'_>,
        method: String,
        fields: Py<PyAny>,
        file_field: String,
        filename: String,
        content: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        host::coroutine(py, async move {
            let form = call(&module("aiohttp")?, "FormData", vec![], None)?;
            let fields = value(&fields)?;
            for (key, val) in fields
                .as_object()
                .ok_or_else(|| PyValueError::new_err("Expected upload fields"))?
            {
                let text = if val.is_array() || val.is_object() {
                    val.to_string()
                } else if let Some(v) = val.as_str() {
                    v.into()
                } else {
                    val.to_string()
                };
                call(
                    &form,
                    "add_field",
                    vec![j(json!(key))?, j(json!(text))?],
                    None,
                )?;
            }
            call(
                &form,
                "add_field",
                vec![j(json!(file_field))?, content],
                Some(j(
                    json!({"filename":filename,"content_type":"application/octet-stream"}),
                )?),
            )?;
            let kwargs = Python::attach(|py| -> PyResult<_> {
                let kw = PyDict::new(py);
                kw.set_item("data", form)?;
                Ok(kw.into_any().unbind())
            })?;
            post(&slf.into_any(), &method, 60., kwargs).await
        })
    }
    #[pyo3(signature=(file_id,*,max_bytes))]
    fn download_file(
        slf: Py<Self>,
        py: Python<'_>,
        file_id: String,
        max_bytes: usize,
    ) -> PyResult<Py<PyAny>> {
        host::coroutine(py, async move {
            let this = slf.into_any();
            let file = acall(
                &this,
                "call",
                vec![j(json!("getFile"))?, j(json!({"file_id":file_id}))?],
                None,
            )
            .await?;
            let path =
                policy::download_path(&value(&file)?, max_bytes).map_err(PyValueError::new_err)?;
            let session = open_session(&this)?;
            let encoded = extract::<String>(&call(
                &module("urllib.parse")?,
                "quote",
                vec![j(json!(path))?],
                None,
            )?)?;
            let url = format!(
                "{}/file/bot{}/{}",
                extract::<String>(&get(&this, "_api_root")?)?,
                extract::<String>(&get(&this, "_token")?)?,
                encoded
            );
            let timeout = call(
                &module("aiohttp")?,
                "ClientTimeout",
                vec![],
                Some(j(json!({"total":60}))?),
            )?;
            let kwargs = Python::attach(|py| -> PyResult<_> {
                let kw = PyDict::new(py);
                kw.set_item("timeout", timeout)?;
                kw.set_item("allow_redirects", false)?;
                Ok(kw.into_any().unbind())
            })?;
            let result = async {
                let context = call(&session, "get", vec![j(json!(url))?], Some(kwargs))?;
                let response = acall(&context, "__aenter__", vec![], None).await?;
                let result = async {
                    let status = extract::<i64>(&get(&response, "status")?)?;
                    if status != 200 {
                        return Err(PyRuntimeError::new_err(format!(
                            "Telegram file download failed (HTTP {status})"
                        )));
                    }
                    let iterator = call(
                        &get(&response, "content")?,
                        "iter_chunked",
                        vec![j(json!(65536))?],
                        None,
                    )?;
                    let mut bytes = Vec::new();
                    loop {
                        match acall(&iterator, "__anext__", vec![], None).await {
                            Ok(chunk) => {
                                let chunk = extract::<Vec<u8>>(&chunk)?;
                                if bytes.len() + chunk.len() > max_bytes {
                                    return Err(PyValueError::new_err(
                                        policy::download_limit_error(max_bytes),
                                    ));
                                }
                                bytes.extend(chunk);
                            }
                            Err(error) => {
                                if Python::attach(|py| {
                                    error.is_instance_of::<PyStopAsyncIteration>(py)
                                }) {
                                    break;
                                }
                                return Err(error);
                            }
                        }
                    }
                    Python::attach(|py| Ok(PyBytes::new(py, &bytes).into_any().unbind()))
                }
                .await;
                exit_context(&context, result).await
            }
            .await
            .map_err(|e| transport_failure(e, "Telegram file download failed"))?;
            call(
                &module("agentchat.channels.telegram.transport")?,
                "TelegramFile",
                vec![j(json!(path))?, result],
                None,
            )
        })
    }
    fn close(slf: Py<Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        host::coroutine(py, async move {
            let this = slf.into_any();
            set(&this, "_closed", j(json!(true))?)?;
            let session = get(&this, "_session")?;
            if !is_none(&session) {
                acall(&session, "close", vec![], None).await?;
            }
            Ok(none())
        })
    }
}
fn open_session(this: &Py<PyAny>) -> PyResult<Py<PyAny>> {
    if extract::<bool>(&get(this, "_closed")?)? {
        return Err(api_error("Telegram client is closed", None, None));
    }
    let mut session = get(this, "_session")?;
    if is_none(&session) {
        session = call(&module("aiohttp")?, "ClientSession", vec![], None)?;
        set(this, "_session", clone_obj(&session))?;
    }
    Ok(session)
}
async fn post(
    this: &Py<PyAny>,
    method: &str,
    timeout: f64,
    kwargs: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    let session = open_session(this)?;
    let url = format!(
        "{}/bot{}/{method}",
        extract::<String>(&get(this, "_api_root")?)?,
        extract::<String>(&get(this, "_token")?)?
    );
    let timeout = call(
        &module("aiohttp")?,
        "ClientTimeout",
        vec![],
        Some(j(json!({"total":timeout}))?),
    )?;
    Python::attach(|py| -> PyResult<()> {
        kwargs.bind(py).set_item("allow_redirects", false)?;
        kwargs.bind(py).set_item("timeout", timeout)
    })?;
    let result = async {
        let context = call(&session, "post", vec![j(json!(url))?], Some(kwargs))?;
        let response = acall(&context, "__aenter__", vec![], None).await?;
        let result = async {
            let status = extract::<i64>(&get(&response, "status")?)?;
            let body = match acall(
                &response,
                "json",
                vec![],
                Some(j(json!({"content_type":null}))?),
            )
            .await
            {
                Ok(v) => value(&v)?,
                Err(e) if Python::attach(|py| e.is_instance_of::<PyValueError>(py)) => {
                    serde_json::Value::Null
                }
                Err(e) => return Err(e),
            };
            let result = policy::unwrap_result(method, status, &body)
                .map_err(|e| api_error(&e.description, e.error_code, e.retry_after))?;
            j(result)
        }
        .await;
        exit_context(&context, result).await
    }
    .await;
    result.map_err(|e| {
        transport_failure(
            e,
            &format!("Telegram {method} request failed; delivery may be uncertain"),
        )
    })
}
pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    host::register_initialized_class::<TelegramBotClient>(module)?;
    module.add_function(wrap_pyfunction!(telegram_error_init, module)?)?;
    module.add_function(wrap_pyfunction!(telegram_error_transient, module)?)?;
    Ok(())
}
