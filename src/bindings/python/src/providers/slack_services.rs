//! Python callbacks and exception translation for native Slack HTTP services.
use super::{slack_files::policy_error, transport::*};
use crate::{convert, host};
use agentchat_slack::{
    delivery::{self, DeliveryResult, Reconciliation, StatusRequest},
    message_request::MessageRequest,
    policy::ReplyDestination,
    webhook::{SlackWebhook as CoreWebhook, WebhookError},
};
use pyo3::{
    create_exception,
    exceptions::{
        PyConnectionError, PyException, PyPermissionError, PyTimeoutError, PyTypeError,
        PyValueError,
    },
    prelude::*,
    types::{PyBytes, PyTuple, PyType},
};
use serde_json::json;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

create_exception!(agentchat, SlackSignatureError, PyPermissionError);
create_exception!(agentchat, SlackPayloadError, PyValueError);
create_exception!(agentchat, SlackBodyTooLarge, SlackPayloadError);
fn webhook_error(error: WebhookError) -> PyErr {
    match error {
        WebhookError::Signature => SlackSignatureError::new_err(error.to_string()),
        WebhookError::Payload => SlackPayloadError::new_err(error.to_string()),
        WebhookError::TooLarge => SlackBodyTooLarge::new_err(error.to_string()),
    }
}
fn headers(value: &Bound<'_, PyAny>) -> PyResult<(String, String)> {
    let mut stamp = None;
    let mut signature = None;
    for pair in value.call_method0("items")?.try_iter()? {
        let (key, value): (String, String) = pair?.extract()?;
        let slot = if key.eq_ignore_ascii_case("x-slack-request-timestamp") {
            &mut stamp
        } else if key.eq_ignore_ascii_case("x-slack-signature") {
            &mut signature
        } else {
            continue;
        };
        if slot.replace(value).is_some() {
            return Err(webhook_error(WebhookError::Signature));
        }
    }
    Ok((stamp.unwrap_or_default(), signature.unwrap_or_default()))
}
fn current_time(now: Option<f64>) -> f64 {
    now.unwrap_or_else(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(f64::NAN, |duration| duration.as_secs_f64())
    })
}

#[pyclass(frozen, module = "agentchat.channels.slack.webhook")]
struct SlackWebhook {
    inner: CoreWebhook,
}
#[pymethods]
impl SlackWebhook {
    #[new]
    fn new(secret: &str) -> Self {
        Self {
            inner: CoreWebhook::new(secret),
        }
    }
    #[pyo3(signature=(raw,headers,*,now=None))]
    fn verify(
        &self,
        raw: &Bound<'_, PyBytes>,
        headers: &Bound<'_, PyAny>,
        now: Option<f64>,
    ) -> PyResult<()> {
        let (stamp, signature) = self::headers(headers)?;
        self.inner
            .verify(raw.as_bytes(), &stamp, &signature, current_time(now))
            .map_err(webhook_error)
    }
    #[pyo3(signature=(raw,headers,*,now=None,max_bytes=262_144))]
    fn event(
        &self,
        py: Python<'_>,
        raw: &Bound<'_, PyBytes>,
        headers: &Bound<'_, PyAny>,
        now: Option<f64>,
        max_bytes: usize,
    ) -> PyResult<Py<PyAny>> {
        let (stamp, signature) = self::headers(headers)?;
        let result = self
            .inner
            .event(
                raw.as_bytes(),
                &stamp,
                &signature,
                current_time(now),
                max_bytes,
            )
            .map_err(webhook_error)?;
        convert::from_json(py, &json!(result))
    }
    #[pyo3(signature=(raw,headers,*,now=None,max_bytes=65_536))]
    fn interaction(
        &self,
        py: Python<'_>,
        raw: &Bound<'_, PyBytes>,
        headers: &Bound<'_, PyAny>,
        now: Option<f64>,
        max_bytes: usize,
    ) -> PyResult<Py<PyAny>> {
        let (stamp, signature) = self::headers(headers)?;
        let result = self
            .inner
            .interaction(
                raw.as_bytes(),
                &stamp,
                &signature,
                current_time(now),
                max_bytes,
            )
            .map_err(webhook_error)?;
        convert::from_json(py, &result)
    }
}

#[pyclass(frozen, module = "agentchat.channels.slack.delivery")]
struct SlackDelivery {
    request: Py<PyAny>,
    transport_errors: Py<PyTuple>,
}
impl SlackDelivery {
    fn send_prepared(&self, py: Python<'_>, prepared: MessageRequest) -> PyResult<Py<PyAny>> {
        let request = self.request.clone_ref(py);
        let errors = self.transport_errors.clone_ref(py);
        host::coroutine(py, async move {
            let method = serde_json::to_value(prepared.method)
                .map_err(|_| PyValueError::new_err("Invalid Slack message method"))?;
            let response = acall(
                &request,
                "__call__",
                vec![j(json!("POST"))?, j(method)?, j(prepared.payload.clone())?],
                None,
            )
            .await;
            let outcome = match response {
                Ok(response) => match value(&response) {
                    Ok(response) => DeliveryResult::from_response(&prepared, response),
                    Err(_) => DeliveryResult::uncertain("invalid_response"),
                },
                Err(error) if is_transport_error(&error, &errors) => {
                    DeliveryResult::uncertain("transport")
                }
                Err(error) => return Err(error),
            };
            j(json!(outcome))
        })
    }
}
fn is_transport_error(error: &PyErr, errors: &Py<PyTuple>) -> bool {
    Python::attach(|py| {
        error
            .value(py)
            .is_instance(errors.bind(py))
            .unwrap_or(false)
    })
}
#[pymethods]
impl SlackDelivery {
    #[new]
    #[pyo3(signature=(request,*,transport_errors=None))]
    fn new(
        py: Python<'_>,
        request: Py<PyAny>,
        transport_errors: Option<&Bound<'_, PyTuple>>,
    ) -> PyResult<Self> {
        if !request.bind(py).is_callable() {
            return Err(PyTypeError::new_err(
                "Expected an async Slack request callback",
            ));
        }
        let errors = match transport_errors {
            Some(errors) => errors.clone(),
            None => PyTuple::new(
                py,
                [
                    py.get_type::<PyConnectionError>(),
                    py.get_type::<PyTimeoutError>(),
                ],
            )?,
        };
        for error in errors.iter() {
            let valid = error.cast::<PyType>().is_ok_and(|kind| {
                kind.is_subclass(&py.get_type::<PyException>())
                    .unwrap_or(false)
            });
            if !valid {
                return Err(PyTypeError::new_err(
                    "Transport errors must be Exception classes",
                ));
            }
        }
        Ok(Self {
            request,
            transport_errors: errors.unbind(),
        })
    }
    fn set_status(
        &self,
        py: Python<'_>,
        channel_id: &str,
        thread_ts: &str,
        status: &str,
    ) -> PyResult<Py<PyAny>> {
        let prepared = StatusRequest::new(
            ReplyDestination::validate(channel_id, Some(thread_ts)).map_err(policy_error)?,
            status,
        );
        let request = self.request.clone_ref(py);
        let errors = self.transport_errors.clone_ref(py);
        host::coroutine(py, async move {
            let response = acall(
                &request,
                "__call__",
                vec![
                    j(json!("POST"))?,
                    j(json!(prepared.method))?,
                    j(prepared.payload)?,
                ],
                None,
            )
            .await;
            let result = match response {
                Ok(response) => match value(&response) {
                    Ok(response) => StatusRequest::response(response),
                    Err(_) => DeliveryResult::uncertain("invalid_response"),
                },
                Err(error) if is_transport_error(&error, &errors) => {
                    DeliveryResult::uncertain("transport")
                }
                Err(error) => return Err(error),
            };
            j(json!(result))
        })
    }
    fn send_request(&self, py: Python<'_>, request: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let prepared =
            delivery::prepare_request(&convert::to_json(request)?).map_err(policy_error)?;
        self.send_prepared(py, prepared)
    }
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature=(reply,*,channel_id,thread_ts=None,message_ts=None,metadata=None,fallback_blocks=true))]
    fn send(
        &self,
        py: Python<'_>,
        reply: &Bound<'_, PyAny>,
        channel_id: &str,
        thread_ts: Option<&str>,
        message_ts: Option<&str>,
        metadata: Option<&Bound<'_, PyAny>>,
        fallback_blocks: bool,
    ) -> PyResult<Py<PyAny>> {
        let reply = convert::from_rich_reply(reply)?;
        let metadata = metadata.map(convert::to_json).transpose()?;
        let prepared = MessageRequest::new(
            &reply,
            channel_id,
            thread_ts,
            message_ts,
            metadata.as_ref(),
            fallback_blocks,
        )
        .map_err(policy_error)?;
        self.send_prepared(py, prepared)
    }
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature=(channel_id,thread_ts,*,event_type,event_id,max_pages=5,timeout=8.0))]
    fn reconcile(
        &self,
        py: Python<'_>,
        channel_id: &str,
        thread_ts: &str,
        event_type: &str,
        event_id: &str,
        max_pages: usize,
        timeout: f64,
    ) -> PyResult<Py<PyAny>> {
        if !timeout.is_finite() || timeout <= 0.0 {
            return Err(PyValueError::new_err("Expected a positive finite timeout"));
        }
        let mut reader =
            Reconciliation::new(channel_id, thread_ts, event_type, event_id, max_pages)
                .map_err(policy_error)?;
        let request = self.request.clone_ref(py);
        let errors = self.transport_errors.clone_ref(py);
        host::coroutine(py, async move {
            let started = Instant::now();
            loop {
                let remaining = timeout - started.elapsed().as_secs_f64();
                if remaining <= 0.0 {
                    reader.fail("timeout");
                    break;
                }
                let pending = call(
                    &request,
                    "__call__",
                    vec![
                        j(json!("GET"))?,
                        j(json!("conversations.replies"))?,
                        j(reader.request())?,
                    ],
                    None,
                )?;
                let pending = call(
                    &module("asyncio")?,
                    "wait_for",
                    vec![pending],
                    Some(j(json!({"timeout":remaining}))?),
                )?;
                let response = match host::await_py(pending).await {
                    Ok(response) => response,
                    Err(error)
                        if Python::attach(|py| error.is_instance_of::<PyTimeoutError>(py)) =>
                    {
                        reader.fail("timeout");
                        break;
                    }
                    Err(error) if is_transport_error(&error, &errors) => {
                        reader.fail("transport");
                        break;
                    }
                    Err(error) => return Err(error),
                };
                match value(&response) {
                    Ok(response) => {
                        if reader.accept(&response) {
                            break;
                        }
                    }
                    Err(_) => {
                        reader.fail("invalid_response");
                        break;
                    }
                }
            }
            j(json!(reader.finish()))
        })
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<SlackWebhook>()?;
    module.add_class::<SlackDelivery>()?;
    module.add(
        "SlackSignatureError",
        module.py().get_type::<SlackSignatureError>(),
    )?;
    module.add(
        "SlackPayloadError",
        module.py().get_type::<SlackPayloadError>(),
    )?;
    module.add(
        "SlackBodyTooLarge",
        module.py().get_type::<SlackBodyTooLarge>(),
    )?;
    Ok(())
}
