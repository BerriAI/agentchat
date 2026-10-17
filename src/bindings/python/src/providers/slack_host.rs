//! Host callbacks run on the caller's asyncio loop; Slack interpretation lives in the SDK.
use super::{slack_files::policy_error, transport::*};
use crate::{convert, host};
use agentchat_slack::host::{
    ack_accepted, ContextLimits, CurrentReference, SlackHost as CoreHost, ThreadContextReader,
    ThreadOrigin,
};
use pyo3::{
    exceptions::{PyException, PyTimeoutError, PyValueError},
    prelude::*,
};
use serde_json::json;
use std::time::Instant;

#[pyclass(frozen, module = "agentchat.channels.slack.host")]
struct SlackHost {
    inner: CoreHost,
}
#[pymethods]
impl SlackHost {
    #[new]
    #[pyo3(signature=(*,bot_user_id,workspace_id=None,exclude_prefixes=Vec::new(),ack_emoji=Some("eyes")))]
    fn new(
        bot_user_id: String,
        workspace_id: Option<String>,
        exclude_prefixes: Vec<String>,
        ack_emoji: Option<&str>,
    ) -> Self {
        Self {
            inner: CoreHost {
                bot_user_id,
                workspace_id,
                exclude_prefixes,
                ack_emoji: ack_emoji.map(str::to_owned),
            },
        }
    }
    fn routing_text(&self, py: Python<'_>, text: &str) -> PyResult<Py<PyAny>> {
        convert::from_json(py, &json!(self.inner.routing_text(text)))
    }
    fn normalize_event(&self, py: Python<'_>, payload: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let event = self
            .inner
            .normalize_event(&convert::to_json(payload)?)
            .map_err(policy_error)?;
        convert::from_json(py, &json!(event))
    }
    #[pyo3(signature=(event,request,*,timeout=3.0))]
    fn acknowledge(
        &self,
        py: Python<'_>,
        event: &Bound<'_, PyAny>,
        request: Py<PyAny>,
        timeout: f64,
    ) -> PyResult<Py<PyAny>> {
        if !timeout.is_finite() || timeout <= 0.0 {
            return Err(PyValueError::new_err("Expected a positive finite timeout"));
        }
        let source = convert::to_json(event)
            .map_err(|_| PyValueError::new_err("Invalid Slack event origin"))?;
        let origin: ThreadOrigin = serde_json::from_value(source)
            .map_err(|_| PyValueError::new_err("Invalid Slack event origin"))?;
        let payload = self.inner.acknowledgement(&origin);
        host::coroutine(py, async move {
            let Some(payload) = payload else {
                return j(json!(false));
            };
            let args = vec![j(json!("reactions.add"))?, j(payload)?];
            let pending = match call(&request, "__call__", args, None) {
                Ok(pending) => pending,
                Err(error) if Python::attach(|py| error.is_instance_of::<PyException>(py)) => {
                    return j(json!(false));
                }
                Err(error) => return Err(error),
            };
            let bounded = call(
                &module("asyncio")?,
                "wait_for",
                vec![pending],
                Some(j(json!({"timeout":timeout}))?),
            )?;
            let response = match host::await_py(bounded).await {
                Ok(response) => response,
                Err(error) if Python::attach(|py| error.is_instance_of::<PyException>(py)) => {
                    return j(json!(false));
                }
                Err(error) => return Err(error),
            };
            j(json!(ack_accepted(&value(&response)?)))
        })
    }
    // Explicit Python keyword arguments are the public FFI contract.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature=(origin,request_page=None,*,max_pages=5,max_chars=40_000,max_message_chars=8_000,timeout=8.0))]
    fn thread_context(
        &self,
        py: Python<'_>,
        origin: &Bound<'_, PyAny>,
        request_page: Option<Py<PyAny>>,
        max_pages: usize,
        max_chars: usize,
        max_message_chars: usize,
        timeout: f64,
    ) -> PyResult<Py<PyAny>> {
        if !timeout.is_finite() || timeout <= 0.0 {
            return Err(PyValueError::new_err("Expected a positive finite timeout"));
        }
        let source = convert::to_json(origin)?;
        let origin: ThreadOrigin = serde_json::from_value(source.clone())
            .map_err(|_| PyValueError::new_err("Invalid Slack thread origin"))?;
        let mut reader = ThreadContextReader::new(
            self.inner.clone(),
            origin,
            ContextLimits {
                max_pages,
                max_chars,
                max_message_chars,
            },
        )
        .map_err(policy_error)?;
        if let Some(reference) = source.get("reference") {
            let reference: CurrentReference = serde_json::from_value(reference.clone())
                .map_err(|_| PyValueError::new_err("Invalid Slack current reference"))?;
            let user_id = source["user_id"]
                .as_str()
                .ok_or_else(|| PyValueError::new_err("Expected Slack reference author"))?;
            reader.seed(reference, user_id).map_err(policy_error)?;
        }
        host::coroutine(py, async move {
            let Some(request_page) = request_page else {
                reader.fail("history_not_requested");
                return j(json!(reader.finish()));
            };
            let started = Instant::now();
            loop {
                let remaining = timeout - started.elapsed().as_secs_f64();
                if remaining <= 0.0 {
                    reader.fail("timeout");
                    break;
                }
                let pending = call(&request_page, "__call__", vec![j(reader.request())?], None)?;
                let bounded = call(
                    &module("asyncio")?,
                    "wait_for",
                    vec![pending],
                    Some(j(json!({"timeout":remaining}))?),
                )?;
                let result = host::await_py(bounded).await;
                let response = match result {
                    Ok(response) => response,
                    Err(error)
                        if Python::attach(|py| error.is_instance_of::<PyTimeoutError>(py)) =>
                    {
                        reader.fail("timeout");
                        break;
                    }
                    Err(error) => return Err(error),
                };
                if reader.accept(&value(&response)?) {
                    break;
                }
            }
            j(json!(reader.finish()))
        })
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SlackHost>()
}
