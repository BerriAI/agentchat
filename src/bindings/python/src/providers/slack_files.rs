//! Foreign HTTP effects; all transfer validation and sequencing lives in agentchat-slack.
use super::transport::*;
use crate::{convert, host};
use agentchat_core::{Error, UploadFile};
use agentchat_slack::file_transfers::{self, reader::FileMetadata, FileTransport, TransferError};
use async_trait::async_trait;
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
    types::{PyBool, PyBytes, PyTuple},
};
use serde_json::{json, Value};

pub(super) fn policy_error(error: Error) -> PyErr {
    match error {
        Error::InvalidInput(message) => PyValueError::new_err(message),
        other => PyRuntimeError::new_err(other.to_string()),
    }
}
fn transfer_error(error: TransferError<PyErr>) -> PyErr {
    match error {
        TransferError::Policy(error) => policy_error(error),
        TransferError::Transport(error) => error,
    }
}

pub(super) fn files_from_python(files: &Bound<'_, PyAny>) -> PyResult<Vec<UploadFile>> {
    files
        .try_iter()?
        .map(|item| {
            let item = item?;
            let filename = item
                .getattr("filename")?
                .extract::<String>()
                .map_err(|_| PyValueError::new_err("Expected a plain filename"))?;
            let content = item
                .getattr("content")?
                .cast::<PyBytes>()
                .map_err(|_| PyValueError::new_err("Expected nonempty file bytes"))?
                .as_bytes()
                .to_vec();
            let title = item
                .getattr("title")?
                .extract::<Option<String>>()
                .map_err(|_| PyValueError::new_err("Expected a file title"))?;
            Ok(UploadFile {
                filename,
                content,
                title,
            })
        })
        .collect()
}
pub(super) fn byte_limit(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    if value.is_instance_of::<PyBool>() {
        return Err(PyValueError::new_err(
            "Expected a positive attachment byte limit",
        ));
    }
    value
        .extract::<usize>()
        .ok()
        .filter(|v| *v > 0)
        .ok_or_else(|| PyValueError::new_err("Expected a positive attachment byte limit"))
}
pub(super) struct PythonFiles {
    pub request: Py<PyAny>,
    pub guard: Option<Py<PyAny>>,
    pub sdk: bool,
}
#[async_trait]
impl FileTransport for PythonFiles {
    type Error = PyErr;
    async fn request(&self, method: &str, payload: &Value) -> PyResult<Value> {
        let response = if self.sdk {
            let key = match method {
                "files.info" => "params",
                "files.getUploadURLExternal" => "data",
                _ => "json",
            };
            let mut kwargs = json!({"http_verb":if method=="files.info"{"GET"}else{"POST"}});
            kwargs[key] = payload.clone();
            acall(
                &self.request,
                "api_call",
                vec![j(json!(method))?],
                Some(j(kwargs)?),
            )
            .await?
        } else {
            acall(
                &self.request,
                "__call__",
                vec![j(json!(method))?, j(payload.clone())?],
                None,
            )
            .await?
        };
        value(&response)
    }
    async fn upload(&self, url: &str, bytes: &[u8]) -> PyResult<()> {
        let raw = Python::attach(|py| PyBytes::new(py, bytes).into_any().unbind());
        acall(
            &module("agentchat.channels.slack.files")?,
            "upload_bytes",
            vec![j(json!(url))?, raw],
            None,
        )
        .await?;
        Ok(())
    }
    async fn download(&self, url: &str, token: &str, max_bytes: usize) -> PyResult<Vec<u8>> {
        self.download_file(&FileMetadata::location(url), token, max_bytes)
            .await
    }
    async fn download_file(
        &self,
        file: &FileMetadata,
        token: &str,
        max_bytes: usize,
    ) -> PyResult<Vec<u8>> {
        super::slack_reader::download_bytes(file, token, max_bytes).await
    }
    async fn guard(&self) -> PyResult<()> {
        if let Some(guard) = &self.guard {
            acall(guard, "__call__", vec![], None).await?;
        }
        Ok(())
    }
}
async fn session(timeout: u64) -> PyResult<Py<PyAny>> {
    let aiohttp = get(&module("agentchat.channels.slack.files")?, "aiohttp")?;
    let timeout = call(
        &aiohttp,
        "ClientTimeout",
        vec![],
        Some(j(json!({"total":timeout}))?),
    )?;
    let kwargs = dict();
    Python::attach(|py| kwargs.bind(py).set_item("timeout", timeout))?;
    let session = call(&aiohttp, "ClientSession", vec![], Some(kwargs))?;
    Ok(session)
}
#[pyfunction]
fn slack_upload_bytes(py: Python<'_>, url: String, raw: Vec<u8>) -> PyResult<Py<PyAny>> {
    host::coroutine(py, async move {
        file_transfers::validate_file_url(&url, true).map_err(policy_error)?;
        let session = session(60).await?;
        let client = acall(&session, "__aenter__", vec![], None).await?;
        let result=async {
            let kwargs=j(json!({"headers":{"Content-Type":"application/octet-stream"},"allow_redirects":false}))?;
            Python::attach(|py|kwargs.bind(py).set_item("data",PyBytes::new(py,&raw)))?;
            let response=call(&client,"post",vec![j(json!(url))?],Some(kwargs))?;
            let received=acall(&response,"__aenter__",vec![],None).await?;
            let result=if extract::<u16>(&get(&received,"status")?)?==200 {Ok(none())}else{Err(PyRuntimeError::new_err("Slack did not confirm the capture upload"))};
            exit_context(&response,result).await
        }.await;
        exit_context(&session, result).await
    })
}
pub(super) async fn upload(
    files: Vec<UploadFile>,
    channel_id: String,
    thread_ts: Option<String>,
    transport: PythonFiles,
) -> PyResult<Py<PyAny>> {
    let uploaded = file_transfers::upload(&files, &channel_id, thread_ts.as_deref(), &transport)
        .await
        .map_err(transfer_error)?;
    Python::attach(|py| {
        let class = py.import("agentchat.models")?.getattr("UploadedFile")?;
        Ok(PyTuple::new(
            py,
            uploaded
                .iter()
                .map(|file| class.call1((&file.id, &file.permalink)))
                .collect::<PyResult<Vec<_>>>()?,
        )?
        .into_any()
        .unbind())
    })
}
pub(super) async fn download(
    attachment: agentchat_core::Attachment,
    bot_token: String,
    max_bytes: usize,
    transport: PythonFiles,
) -> PyResult<Py<PyAny>> {
    let file = file_transfers::download(&attachment, &bot_token, max_bytes, &transport)
        .await
        .map_err(transfer_error)?;
    Python::attach(|py| {
        Ok(py
            .import("agentchat.models")?
            .getattr("DownloadedFile")?
            .call1((
                &file.id,
                &file.name,
                PyBytes::new(py, &file.content),
                file.media_type,
            ))?
            .unbind())
    })
}
#[pyfunction]
#[pyo3(signature=(files,*,request,channel_id,thread_ts=None,before_send=None))]
fn slack_upload_files(
    py: Python<'_>,
    files: &Bound<'_, PyAny>,
    request: Py<PyAny>,
    channel_id: String,
    thread_ts: Option<String>,
    before_send: Option<Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let files = files_from_python(files)?;
    host::coroutine(
        py,
        upload(
            files,
            channel_id,
            thread_ts,
            PythonFiles {
                request,
                guard: before_send,
                sdk: false,
            },
        ),
    )
}
#[pyfunction]
#[pyo3(signature=(attachment,*,bot_token,request,max_bytes=None,before_download=None))]
fn slack_download_file(
    py: Python<'_>,
    attachment: &Bound<'_, PyAny>,
    bot_token: String,
    request: Py<PyAny>,
    max_bytes: Option<&Bound<'_, PyAny>>,
    before_download: Option<Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let attachment = convert::from_attachment(attachment)?;
    let max_bytes = max_bytes
        .map(byte_limit)
        .transpose()?
        .unwrap_or(10 * 1024 * 1024);
    host::coroutine(
        py,
        download(
            attachment,
            bot_token,
            max_bytes,
            PythonFiles {
                request,
                guard: before_download,
                sdk: false,
            },
        ),
    )
}
#[pyfunction]
fn slack_validate_destination(
    channel_id: &Bound<'_, PyAny>,
    thread_ts: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let channel = channel_id
        .extract::<String>()
        .map_err(|_| PyValueError::new_err("Expected a Slack channel destination"))?;
    let thread = thread_ts
        .extract::<Option<String>>()
        .map_err(|_| PyValueError::new_err("Expected a Slack thread destination"))?;
    agentchat_slack::policy::ReplyDestination::validate(&channel, thread.as_deref())
        .map(|_| ())
        .map_err(policy_error)
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(slack_upload_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(slack_upload_files, module)?)?;
    module.add_function(wrap_pyfunction!(slack_download_file, module)?)?;
    module.add_function(wrap_pyfunction!(slack_validate_destination, module)?)?;
    module
        .getattr("slack_upload_bytes")?
        .setattr("__module__", "agentchat.channels.slack.files")?;
    module
        .getattr("slack_upload_files")?
        .setattr("__module__", "agentchat.channels.slack.files")?;
    module
        .getattr("slack_download_file")?
        .setattr("__module__", "agentchat.channels.slack.files")?;
    module
        .getattr("slack_validate_destination")?
        .setattr("__module__", "agentchat.channels.slack.files")?;
    Ok(())
}
