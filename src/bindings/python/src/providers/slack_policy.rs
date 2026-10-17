//! Slack policy exports. Interpretation is owned by agentchat-slack.
use super::slack_files::policy_error;
use crate::convert;
use agentchat_slack::{events, rendering};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyTuple};
use serde_json::Value;
#[pyfunction]
#[pyo3(signature=(files,*,limit=5))]
fn slack_attachments(
    py: Python<'_>,
    files: &Bound<'_, PyAny>,
    limit: usize,
) -> PyResult<Py<PyAny>> {
    if !(1..=100).contains(&limit) {
        return Err(PyValueError::new_err(
            "Attachment limit must be between 1 and 100",
        ));
    }
    let files = convert::to_json(files)?;
    Ok(PyTuple::new(
        py,
        events::attachments_bounded(&files, limit)
            .iter()
            .map(|file| convert::to_attachment(py, file))
            .collect::<PyResult<Vec<_>>>()?,
    )?
    .into_any()
    .unbind())
}
#[pyfunction]
fn slack_filename(value: &str) -> String {
    events::filename(value)
}
#[pyfunction]
#[pyo3(signature=(payload,event,*,bot_user_id,workspace_id,channel_name="slack"))]
fn slack_to_message(
    py: Python<'_>,
    payload: &Bound<'_, PyAny>,
    event: &Bound<'_, PyAny>,
    bot_user_id: Option<&str>,
    workspace_id: Option<&str>,
    channel_name: &str,
) -> PyResult<Py<PyAny>> {
    let mut payload = convert::to_json(payload)?;
    if let Some(object) = payload.as_object_mut() {
        object.insert("event".into(), convert::to_json(event)?);
    }
    events::normalize_scoped(&payload, bot_user_id, workspace_id, channel_name)
        .as_ref()
        .map(|message| convert::to_message(py, message))
        .unwrap_or_else(|| Ok(py.None()))
}
#[pyfunction]
#[pyo3(signature=(text,*,limit=2992))]
fn slack_text_blocks(py: Python<'_>, text: &str, limit: usize) -> PyResult<Py<PyAny>> {
    convert::from_json(
        py,
        &Value::Array(rendering::text_blocks_with_limit(text, limit).map_err(policy_error)?),
    )
}
#[pyfunction]
#[pyo3(signature=(text,*,mentions=None,resolve_link=None))]
fn slack_markdown_text(
    text: &str,
    mentions: Option<&Bound<'_, PyAny>>,
    resolve_link: Option<&Bound<'_, PyAny>>,
) -> PyResult<String> {
    let allowed_mentions = mentions
        .map(|values| {
            values
                .try_iter()?
                .map(|value| value?.extract::<String>())
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    rendering::markdown_text(
        text,
        rendering::MarkdownOptions {
            allowed_mentions: &allowed_mentions,
        },
        |target| {
            resolve_link
                .map(|resolve| resolve.call1((target,))?.extract::<Option<String>>())
                .transpose()
                .map(Option::flatten)
        },
    )
}
#[pyfunction]
#[pyo3(signature=(text,*,limit=2600,trim_boundary_newlines=false))]
fn slack_split_text(
    text: &str,
    limit: usize,
    trim_boundary_newlines: bool,
) -> PyResult<Vec<String>> {
    rendering::split_text(
        text,
        rendering::SplitOptions {
            limit,
            trim_boundary_newlines,
        },
    )
    .map_err(policy_error)
}
#[pyfunction]
fn slack_rich_payload(py: Python<'_>, content: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    convert::from_json(
        py,
        &rendering::rich_payload(&convert::from_rich_reply(content)?).map_err(policy_error)?,
    )
}
#[pyfunction]
#[pyo3(signature=(reply,*,channel_id,thread_ts=None,message_ts=None,metadata=None,fallback_blocks=true))]
fn slack_message_request(
    py: Python<'_>,
    reply: &Bound<'_, PyAny>,
    channel_id: &str,
    thread_ts: Option<&str>,
    message_ts: Option<&str>,
    metadata: Option<&Bound<'_, PyAny>>,
    fallback_blocks: bool,
) -> PyResult<Py<PyAny>> {
    let metadata = metadata.map(convert::to_json).transpose()?;
    let request = agentchat_slack::message_request::MessageRequest::new(
        &convert::from_rich_reply(reply)?,
        channel_id,
        thread_ts,
        message_ts,
        metadata.as_ref(),
        fallback_blocks,
    )
    .map_err(policy_error)?;
    convert::from_json(py, &serde_json::json!(request))
}
#[pyfunction]
#[pyo3(signature=(message,*,origin="web"))]
fn slack_mirror_payload(
    py: Python<'_>,
    message: &Bound<'_, PyAny>,
    origin: &str,
) -> PyResult<Py<PyAny>> {
    convert::from_json(
        py,
        &rendering::mirror_payload(&convert::from_message(message)?, origin)
            .map_err(policy_error)?,
    )
}
#[pyfunction]
#[pyo3(signature=(record,*,bot_user_id=None))]
fn slack_read_mirror(
    py: Python<'_>,
    record: &Bound<'_, PyAny>,
    bot_user_id: Option<&str>,
) -> PyResult<Py<PyAny>> {
    let value = convert::to_json(record)?;
    match bot_user_id.and_then(|id| rendering::read_mirror(&value, id)) {
        Some(mirror) => Ok((
            py.import("agentchat.models")?
                .getattr("Sender")?
                .call1((mirror.sender.id, mirror.sender.display_name))?,
            mirror.text,
        )
            .into_pyobject(py)?
            .into_any()
            .unbind()),
        None => Ok(py.None()),
    }
}
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(slack_attachments, module)?)?;
    module.add_function(wrap_pyfunction!(slack_filename, module)?)?;
    module.add_function(wrap_pyfunction!(slack_to_message, module)?)?;
    module.add_function(wrap_pyfunction!(slack_text_blocks, module)?)?;
    module.add_function(wrap_pyfunction!(slack_markdown_text, module)?)?;
    module.add_function(wrap_pyfunction!(slack_split_text, module)?)?;
    module.add_function(wrap_pyfunction!(slack_rich_payload, module)?)?;
    module.add_function(wrap_pyfunction!(slack_message_request, module)?)?;
    module.add_function(wrap_pyfunction!(slack_mirror_payload, module)?)?;
    module.add_function(wrap_pyfunction!(slack_read_mirror, module)?)?;
    module
        .getattr("slack_attachments")?
        .setattr("__module__", "agentchat.channels.slack.events")?;
    module
        .getattr("slack_filename")?
        .setattr("__module__", "agentchat.channels.slack.events")?;
    module
        .getattr("slack_to_message")?
        .setattr("__module__", "agentchat.channels.slack.events")?;
    module
        .getattr("slack_text_blocks")?
        .setattr("__module__", "agentchat.channels.slack.rendering")?;
    module
        .getattr("slack_markdown_text")?
        .setattr("__module__", "agentchat.channels.slack.rendering")?;
    module
        .getattr("slack_split_text")?
        .setattr("__module__", "agentchat.channels.slack.rendering")?;
    module
        .getattr("slack_rich_payload")?
        .setattr("__module__", "agentchat.channels.slack.rendering")?;
    module
        .getattr("slack_message_request")?
        .setattr("__module__", "agentchat.channels.slack.rendering")?;
    module
        .getattr("slack_mirror_payload")?
        .setattr("__module__", "agentchat.channels.slack.mirroring")?;
    module
        .getattr("slack_read_mirror")?
        .setattr("__module__", "agentchat.channels.slack.mirroring")?;
    Ok(())
}
