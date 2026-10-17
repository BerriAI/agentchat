//! Asyncio/temp-file effects for the shared Slack file reader. No credential storage.
use super::transport::*;
use crate::{convert, host};
use agentchat_slack::file_transfers::{canvas, failure::*, reader::*};
use async_trait::async_trait;
use pyo3::{
    exceptions::{PyStopAsyncIteration, PyTimeoutError, PyValueError},
    prelude::*,
    types::{PyBytes, PyTuple},
};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

fn public(name: &str) -> PyResult<Py<PyAny>> {
    get(&module("agentchat.channels.slack.files")?, name)
}
fn exception(name: &str, args: Vec<Py<PyAny>>) -> PyResult<PyErr> {
    let obj = call(&public(name)?, "__call__", args, None)?;
    Python::attach(|py| Ok(PyErr::from_value(obj.into_bound(py))))
}
fn failure(error: ReadFailure) -> PyErr {
    let result = (|| {
        let exc = call(
            &public("SlackFileError")?,
            "__call__",
            vec![j(json!(error.code))?],
            Some(j(
                json!({"retryable":error.retryable,"retry_after":error.retry_after}),
            )?),
        )?;
        set(&exc, "history", j(json!(error.history))?)?;
        set(&exc, "retry_blocker", j(json!(error.retry_blocker))?)?;
        Python::attach(|py| Ok(PyErr::from_value(exc.into_bound(py))))
    })();
    result.unwrap_or_else(|e| e)
}
fn read_error(error: ReadError<PyErr>) -> PyErr {
    match error {
        ReadError::Failure(e) => failure(e),
        ReadError::Foreign(e) => e,
    }
}
fn is_exception(error: &PyErr, class: &Py<PyAny>) -> bool {
    Python::attach(|py| error.value(py).is_instance(class.bind(py)).unwrap_or(false))
}
fn file_error(value: &Py<PyAny>) -> PyResult<ReadFailure> {
    let code = value_attr(value, "code")?;
    let history = value_attr(value, "history")?;
    serde_json::from_value(json!({
        "code": code,
        "retryable": value_attr(value, "retryable")?,
        "retry_after": value_attr(value, "retry_after")?,
        "history": history,
        "retry_blocker": value_attr(value, "retry_blocker")?,
    }))
    .map_err(|_| PyValueError::new_err("Invalid Slack file diagnostic"))
}
fn value_attr(obj: &Py<PyAny>, key: &str) -> PyResult<Value> {
    value(&get(obj, key)?)
}
fn classify(error: PyErr, state: &ReadState) -> ReadError<PyErr> {
    let result = (|| -> PyResult<Option<ReadFailure>> {
        let object = Python::attach(|py| error.value(py).clone().into_any().unbind());
        if is_exception(&error, &public("SlackFileError")?) {
            return Ok(Some(file_error(&object)?));
        }
        if is_exception(&error, &public("SlackTransportError")?) {
            let transport: TransportFailure =
                serde_json::from_value(value_attr(&object, "details")?)
                    .map_err(|_| PyValueError::new_err("Invalid Slack transport failure"))?;
            update(state, |s| s.http_status = transport.status_code);
            return Ok(Some(transport.into_read_failure(snapshot(state).stage)));
        }
        let aiohttp = module("aiohttp")?;
        let mut matched = None;
        for (name, code, retry) in [
            ("ClientPayloadError", FailureCode::TruncatedStream, true),
            ("ClientSSLError", FailureCode::Tls, false),
            ("ServerFingerprintMismatch", FailureCode::Tls, false),
            ("ClientError", FailureCode::Transport, true),
        ] {
            if is_exception(&error, &get(&aiohttp, name)?) {
                matched = Some(if retry {
                    ReadFailure::retry(code)
                } else {
                    ReadFailure::new(code)
                });
                break;
            }
        }
        if matched.is_none() && Python::attach(|py| error.is_instance_of::<PyTimeoutError>(py)) {
            matched = Some(ReadFailure::retry(FailureCode::Timeout));
        }
        if matched.is_none()
            && Python::attach(|py| error.is_instance_of::<pyo3::exceptions::PyOSError>(py))
        {
            matched = Some(ReadFailure::new(FailureCode::Storage));
        }
        Ok(matched)
    })();
    match result {
        Ok(Some(f)) => ReadError::Failure(f),
        _ => ReadError::Foreign(error),
    }
}

#[pyfunction]
#[pyo3(signature=(obj,code,*,retryable=false,retry_after=None))]
fn slack_file_error_init(
    obj: &Bound<'_, PyAny>,
    code: &str,
    retryable: bool,
    retry_after: Option<f64>,
) -> PyResult<()> {
    let code: FailureCode = serde_json::from_value(json!(code))
        .map_err(|_| PyValueError::new_err("Unknown Slack file failure code"))?;
    obj.setattr("code", convert::from_json(obj.py(), &json!(code))?)?;
    obj.setattr("retryable", retryable)?;
    obj.setattr("retry_after", retry_after)?;
    obj.setattr("history", convert::from_json(obj.py(), &json!([]))?)?;
    obj.setattr("retry_blocker", "")?;
    obj.setattr("args", (code.detail().0,))?;
    Ok(())
}
#[pyfunction]
fn slack_file_diagnostic(obj: Py<PyAny>) -> PyResult<Py<PyAny>> {
    j(file_error(&obj)?.diagnostic())
}
#[pyfunction]
fn slack_file_error_string(obj: Py<PyAny>) -> PyResult<String> {
    Ok(file_error(&obj)?.to_string())
}
#[pyfunction]
#[pyo3(signature=(obj,code,*,status_code=None,dispatched=None,rejected=false,retryable=false,retry_after=None))]
#[allow(clippy::too_many_arguments)]
fn slack_transport_error_init(
    obj: &Bound<'_, PyAny>,
    code: String,
    status_code: Option<u16>,
    dispatched: Option<bool>,
    rejected: bool,
    retryable: bool,
    retry_after: Option<f64>,
) -> PyResult<()> {
    obj.setattr(
        "details",
        convert::from_json(
            obj.py(),
            &json!(TransportFailure {
                code,
                status_code,
                dispatched,
                rejected,
                retryable,
                retry_after
            }),
        )?,
    )?;
    obj.setattr("args", ("Slack transport request failed",))?;
    Ok(())
}
#[pyfunction]
#[pyo3(signature=(text,*,truncated=false))]
fn slack_canvas_bounded_preview(text: &str, truncated: bool) -> String {
    canvas::bounded_preview(text, truncated)
}
#[pyfunction]
fn slack_is_canvas(file: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(canvas::is_canvas(&convert::to_json(file)?))
}
#[pyfunction]
fn slack_inspect_canvas(py: Python<'_>, raw: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let result = (|| {
        call(&raw, "seek", vec![j(json!(0))?], None)?;
        let bytes: Vec<u8> = extract(&call(
            &raw,
            "read",
            vec![j(json!(canvas::PREVIEW_SOURCE_BYTES + 1))?],
            None,
        )?)?;
        let truncated = bytes.len() > canvas::PREVIEW_SOURCE_BYTES;
        let tail = if truncated {
            let end: i64 = extract(&call(&raw, "seek", vec![j(json!(0))?, j(json!(2))?], None)?)?;
            call(&raw, "seek", vec![j(json!((end - 256).max(0)))?], None)?;
            extract::<Vec<u8>>(&call(&raw, "read", vec![j(json!(256))?], None)?)?
        } else {
            Vec::new()
        };
        let text = canvas::preview(
            &bytes[..bytes.len().min(canvas::PREVIEW_SOURCE_BYTES)],
            &tail,
            truncated,
        )
        .map_err(|e| {
            exception(
                "CanvasFormatError",
                vec![j(json!(e.to_string())).unwrap_or_else(|_| none())],
            )
            .unwrap_or_else(|e| e)
        })?;
        Ok(PyTuple::new(
            py,
            [
                j(json!(canvas::MIME))?,
                PyBytes::new(py, b"").into_any().unbind(),
                j(json!(text))?,
            ],
        )?
        .into_any()
        .unbind())
    })();
    let reset = call(&raw, "seek", vec![j(json!(0))?], None);
    match result {
        Err(e) => Err(e),
        Ok(v) => {
            reset?;
            Ok(v)
        }
    }
}

/// Join the owned worker despite repeated cancellation before its file may close.
async fn file_operation(operation: Py<PyAny>, args: Vec<Py<PyAny>>) -> PyResult<Py<PyAny>> {
    let asyncio = module("asyncio")?;
    let mut values = vec![operation];
    values.extend(args);
    let work = call(&asyncio, "to_thread", values, None)?;
    let task = call(&asyncio, "create_task", vec![work], None)?;
    let mut cancelled = None;
    while !extract::<bool>(&call(&task, "done", vec![], None)?)? {
        if let Err(error) = acall(&asyncio, "shield", vec![clone_obj(&task)], None).await {
            if is_exception(&error, &get(&asyncio, "CancelledError")?) {
                if cancelled.is_none() {
                    cancelled = Some(error);
                }
            } else {
                return Err(cancelled.unwrap_or(error));
            }
        }
    }
    let result = call(&task, "result", vec![], None);
    if let Some(error) = cancelled {
        Err(error)
    } else {
        result
    }
}
#[pyfunction]
#[pyo3(signature=(operation,*args))]
fn slack_file_operation(
    py: Python<'_>,
    operation: Py<PyAny>,
    args: &Bound<'_, PyTuple>,
) -> PyResult<Py<PyAny>> {
    let args = args.iter().map(Bound::unbind).collect();
    host::coroutine(py, file_operation(operation, args))
}
struct OwnedFile(Option<Py<PyAny>>);
impl Drop for OwnedFile {
    fn drop(&mut self) {
        if let Some(raw) = self.0.take() {
            let _ = call(&raw, "close", vec![], None);
        }
    }
}
struct PythonReader {
    token: Py<PyAny>,
    request: Py<PyAny>,
    authorize: Py<PyAny>,
}
impl PythonReader {
    async fn callback(
        &self,
        callback: &Py<PyAny>,
        args: Vec<Py<PyAny>>,
        state: &ReadState,
    ) -> Result<Py<PyAny>, ReadError<PyErr>> {
        acall(callback, "__call__", args, None)
            .await
            .map_err(|e| classify(e, state))
    }
    async fn stream(
        file: &FileMetadata,
        token: &str,
        max_bytes: usize,
        state: &ReadState,
    ) -> PyResult<OwnedFile> {
        agentchat_slack::file_transfers::validate_file_url(&file.url, false)
            .map_err(super::slack_files::policy_error)?;
        let aiohttp = get(&module("agentchat.channels.slack.files")?, "aiohttp")?;
        let timeout = call(
            &aiohttp,
            "ClientTimeout",
            vec![],
            Some(j(json!({"total":45,"sock_connect":10,"sock_read":15}))?),
        )?;
        let options = dict();
        set_item(&options, "timeout", timeout)?;
        set_item(&options, "auto_decompress", j(json!(false))?)?;
        let session = call(&aiohttp, "ClientSession", vec![], Some(options))?;
        let client = acall(&session, "__aenter__", vec![], None).await?;
        let result = Self::response(&client, file, token, max_bytes, state).await;
        close_file_context(&session, result).await
    }

    async fn response(
        client: &Py<PyAny>,
        file: &FileMetadata,
        token: &str,
        max_bytes: usize,
        state: &ReadState,
    ) -> PyResult<OwnedFile> {
        let options = j(json!({
            "headers": {"Authorization": format!("Bearer {token}"), "Accept-Encoding": "identity"},
            "allow_redirects": false,
        }))?;
        let context = call(client, "get", vec![j(json!(file.url))?], Some(options))?;
        let response = acall(&context, "__aenter__", vec![], None).await?;
        let result = Self::body(&response, file, max_bytes, state).await;
        close_file_context(&context, result).await
    }

    async fn body(
        response: &Py<PyAny>,
        file: &FileMetadata,
        max_bytes: usize,
        state: &ReadState,
    ) -> PyResult<OwnedFile> {
        let headers = get(response, "headers")?;
        let header = |name: &str| -> PyResult<Option<String>> {
            extract(&call(&headers, "get", vec![j(json!(name))?], None)?)
        };
        let headers = DownloadHeaders {
            status: extract(&get(response, "status")?)?,
            encoding: header("Content-Encoding")?,
            length: header("Content-Length")?,
            content_range: header("Content-Range")?,
            content_type: header("Content-Type")?,
            retry_after: header("Retry-After")?,
        };
        let mut verifier =
            TransferVerifier::new(file, headers, max_bytes, state.clone()).map_err(failure)?;
        let raw = OwnedFile(Some(call(
            &module("tempfile")?,
            "TemporaryFile",
            vec![],
            None,
        )?));
        let chunks = call(
            &get(response, "content")?,
            "iter_chunked",
            vec![j(json!(64 * 1024))?],
            None,
        )?;
        let iterator = call(&chunks, "__aiter__", vec![], None)?;
        loop {
            match acall(&iterator, "__anext__", vec![], None).await {
                Ok(chunk) => {
                    let size = Python::attach(|py| chunk.bind(py).len())?;
                    verifier.accept(size).map_err(failure)?;
                    file_operation(
                        get(raw.0.as_ref().expect("owned file"), "write")?,
                        vec![chunk],
                    )
                    .await?;
                }
                Err(error)
                    if Python::attach(|py| error.is_instance_of::<PyStopAsyncIteration>(py)) =>
                {
                    break
                }
                Err(error) => return Err(error),
            }
        }
        verifier.finish().map_err(failure)?;
        Ok(raw)
    }
}
pub(super) async fn download_bytes(
    file: &FileMetadata,
    token: &str,
    max_bytes: usize,
) -> PyResult<Vec<u8>> {
    let state = Arc::new(Mutex::new(ReadProgress::default()));
    let raw = PythonReader::stream(file, token, max_bytes, &state)
        .await
        .map_err(|error| read_error(classify(error, &state)))?;
    call(
        raw.0.as_ref().expect("verified file"),
        "seek",
        vec![j(json!(0))?],
        None,
    )?;
    extract(&file_operation(get(raw.0.as_ref().expect("verified file"), "read")?, vec![]).await?)
}
fn set_item(dict: &Py<PyAny>, key: &str, value: Py<PyAny>) -> PyResult<()> {
    Python::attach(|py| dict.bind(py).set_item(key, value))
}
async fn close_file_context(
    context: &Py<PyAny>,
    result: PyResult<OwnedFile>,
) -> PyResult<OwnedFile> {
    match result {
        Ok(raw) => {
            exit_context(context, Ok(none())).await?;
            Ok(raw)
        }
        Err(error) => {
            let original = Python::attach(|py| error.clone_ref(py));
            let _ = exit_context(context, Err(error)).await;
            Err(original)
        }
    }
}
#[async_trait]
impl ReadTransport for PythonReader {
    type Error = PyErr;
    type File = OwnedFile;
    async fn authorize(&self) -> Result<(), ReadError<PyErr>> {
        match acall(&self.authorize, "__call__", vec![], None).await {
            Ok(_) => Ok(()),
            Err(error) => {
                if public("SlackFileError").is_ok_and(|class| is_exception(&error, &class)) {
                    let object = Python::attach(|py| error.value(py).clone().into_any().unbind());
                    if let Ok(failure) = file_error(&object) {
                        return Err(ReadError::Failure(failure));
                    }
                }
                Err(ReadError::Foreign(error))
            }
        }
    }
    async fn token(&self, state: &ReadState) -> Result<String, ReadError<PyErr>> {
        let token = self.callback(&self.token, vec![], state).await?;
        extract(&token).map_err(ReadError::Foreign)
    }
    async fn metadata(
        &self,
        id: &str,
        token: &str,
        state: &ReadState,
    ) -> Result<Value, ReadError<PyErr>> {
        let args = (|| {
            Ok(vec![
                j(json!("files.info"))?,
                j(json!({"file":id}))?,
                j(json!(token))?,
            ])
        })()
        .map_err(ReadError::Foreign)?;
        value(&self.callback(&self.request, args, state).await?).map_err(ReadError::Foreign)
    }
    async fn download(
        &self,
        file: &FileMetadata,
        token: &str,
        max_bytes: usize,
        state: &ReadState,
    ) -> Result<OwnedFile, ReadError<PyErr>> {
        let raw = Self::stream(file, token, max_bytes, state)
            .await
            .map_err(|e| classify(e, state))?;
        call(
            raw.0.as_ref().expect("owned file"),
            "seek",
            vec![j(json!(0)).map_err(ReadError::Foreign)?],
            None,
        )
        .map_err(ReadError::Foreign)?;
        Ok(raw)
    }
    async fn sleep(&self, seconds: f64) -> Result<(), ReadError<PyErr>> {
        sleep(seconds).await.map_err(ReadError::Foreign)
    }
}
#[pyclass(frozen, module = "agentchat.channels.slack.files")]
struct SlackFileReader {
    inner: Arc<FileReader>,
}
#[pymethods]
impl SlackFileReader {
    #[new]
    #[pyo3(signature=(*,max_bytes,read_attempts=2,read_budget=90.0,retry_delay=1.0))]
    fn new(
        max_bytes: usize,
        read_attempts: usize,
        read_budget: f64,
        retry_delay: f64,
    ) -> PyResult<Self> {
        Ok(Self {
            inner: Arc::new(
                FileReader::new(max_bytes, read_attempts, read_budget, retry_delay)
                    .map_err(failure)?,
            ),
        })
    }
    #[pyo3(signature=(file_id,*,token,request,authorize))]
    fn read(
        &self,
        py: Python<'_>,
        file_id: String,
        token: Py<PyAny>,
        request: Py<PyAny>,
        authorize: Py<PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let reader = self.inner.clone();
        let budget = reader.budget.as_secs_f64();
        let state = Arc::new(Mutex::new(ReadProgress::default()));
        let capture = state.clone();
        let work = host::coroutine(py, async move {
            let transport = PythonReader {
                token,
                request,
                authorize,
            };
            let mut verified = reader
                .read(&file_id, &transport, &state)
                .await
                .map_err(read_error)?;
            let raw = verified.file.0.as_ref().expect("verified file");
            let result = call(
                &public("SlackDownloadedFile")?,
                "__call__",
                vec![],
                Some({
                    let kwargs = dict();
                    set_item(&kwargs, "name", j(json!(verified.metadata.name))?)?;
                    set_item(&kwargs, "raw", clone_obj(raw))?;
                    set_item(
                        &kwargs,
                        "media_type",
                        j(json!(verified.metadata.media_type))?,
                    )?;
                    set_item(
                        &kwargs,
                        "representation",
                        j(json!(if verified.metadata.canvas {
                            "canvas_html"
                        } else {
                            "file"
                        }))?,
                    )?;
                    set_item(&kwargs, "history", j(json!(verified.history))?)?;
                    set_item(&kwargs, "state", j(json!(verified.state))?)?;
                    kwargs
                }),
            );
            if result.is_ok() {
                verified.file.0.take();
            }
            result
        })?;
        host::coroutine(py, async move {
            let started = std::time::Instant::now();
            match acall(
                &module("asyncio")?,
                "wait_for",
                vec![work],
                Some(j(json!({"timeout":budget}))?),
            )
            .await
            {
                Err(error)
                    if started.elapsed().as_secs_f64() >= budget
                        && Python::attach(|py| error.is_instance_of::<PyTimeoutError>(py)) =>
                {
                    let progress = capture.lock().expect("read state poisoned");
                    let mut error = ReadFailure::new(FailureCode::ReadBudget);
                    error.history = progress.history.clone();
                    let mut last = progress.current.clone();
                    last.code = Some(error.code);
                    error.history.push(last);
                    Err(failure(error))
                }
                result => result,
            }
        })
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SlackFileReader>()?;
    module.add_function(wrap_pyfunction!(slack_file_error_init, module)?)?;
    module.add_function(wrap_pyfunction!(slack_file_diagnostic, module)?)?;
    module.add_function(wrap_pyfunction!(slack_file_error_string, module)?)?;
    module.add_function(wrap_pyfunction!(slack_transport_error_init, module)?)?;
    module.add_function(wrap_pyfunction!(slack_canvas_bounded_preview, module)?)?;
    module.add_function(wrap_pyfunction!(slack_is_canvas, module)?)?;
    module.add_function(wrap_pyfunction!(slack_inspect_canvas, module)?)?;
    module.add_function(wrap_pyfunction!(slack_file_operation, module)?)?;
    module.add("SLACK_CANVAS_MIME", canvas::MIME)?;
    Ok(())
}
