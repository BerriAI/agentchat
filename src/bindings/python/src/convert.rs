//! Foreign-value conversion only. Provider interpretation belongs to provider crates.
use agentchat_core::{Attachment, Message, RichReply, Role, Sender};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple},
};
use serde_json::{Map, Value};

pub fn to_json(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    json_value(value, &mut Vec::new())
}

fn json_value(value: &Bound<'_, PyAny>, ancestors: &mut Vec<usize>) -> PyResult<Value> {
    let identity = value.as_ptr() as usize;
    // Opaque or cyclic application metadata is retained by the host object.
    // It is not wire data and must never recurse through the native stack.
    if ancestors.len() >= 64 || ancestors.contains(&identity) {
        return Ok(Value::Null);
    }
    ancestors.push(identity);
    let result = json_contents(value, ancestors);
    ancestors.pop();
    result
}

fn json_contents(value: &Bound<'_, PyAny>, ancestors: &mut Vec<usize>) -> PyResult<Value> {
    if value.is_none() {
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.extract()?));
    }
    if value.is_instance_of::<PyString>() {
        return Ok(Value::String(value.extract()?));
    }
    if value.is_instance_of::<PyInt>() {
        if let Ok(number) = value.extract::<i64>() {
            return Ok(number.into());
        }
        if let Ok(number) = value.extract::<u64>() {
            return Ok(number.into());
        }
        return Ok(Value::Null);
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(serde_json::Number::from_f64(value.extract()?)
            .map(Value::Number)
            .unwrap_or(Value::Null));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        return value
            .try_iter()?
            .map(|item| json_value(&item?, ancestors))
            .collect::<PyResult<Vec<_>>>()
            .map(Value::Array);
    }
    if value.hasattr("items")? {
        let mut object = Map::new();
        for item in value.call_method0("items")?.try_iter()? {
            let pair = item?;
            if let Ok(key) = pair.get_item(0)?.extract::<String>() {
                object.insert(key, json_value(&pair.get_item(1)?, ancestors)?);
            }
        }
        return Ok(Value::Object(object));
    }
    // SDK response objects expose their decoded wire value through `data`.
    if value.hasattr("data")? {
        return json_value(&value.getattr("data")?, ancestors);
    }
    Ok(Value::Null)
}

pub fn from_json(py: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(value) => value.into_pyobject(py)?.to_owned().into_any().unbind(),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                value.into_pyobject(py)?.into_any().unbind()
            } else if let Some(value) = value.as_u64() {
                value.into_pyobject(py)?.into_any().unbind()
            } else {
                value
                    .as_f64()
                    .ok_or_else(|| PyValueError::new_err("Invalid JSON number"))?
                    .into_pyobject(py)?
                    .into_any()
                    .unbind()
            }
        }
        Value::String(value) => value.into_pyobject(py)?.into_any().unbind(),
        Value::Array(values) => PyList::new(
            py,
            values
                .iter()
                .map(|v| from_json(py, v))
                .collect::<PyResult<Vec<_>>>()?,
        )?
        .into_any()
        .unbind(),
        Value::Object(values) => {
            let object = PyDict::new(py);
            for (key, value) in values {
                object.set_item(key, from_json(py, value)?)?;
            }
            object.into_any().unbind()
        }
    })
}

pub fn to_attachment(py: Python<'_>, attachment: &Attachment) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("agentchat.models")?
        .getattr("Attachment")?
        .call1((
            &attachment.id,
            &attachment.name,
            &attachment.media_type,
            attachment.size,
        ))?
        .unbind())
}

pub fn from_attachment(value: &Bound<'_, PyAny>) -> PyResult<Attachment> {
    Ok(Attachment {
        id: value.getattr("id")?.extract()?,
        name: value.getattr("name")?.extract()?,
        media_type: value.getattr("media_type")?.extract()?,
        size: value.getattr("size")?.extract()?,
    })
}

pub fn to_message(py: Python<'_>, message: &Message) -> PyResult<Py<PyAny>> {
    let models = py.import("agentchat.models")?;
    let sender = models
        .getattr("Sender")?
        .call1((&message.sender.id, &message.sender.display_name))?;
    let metadata = py
        .import("types")?
        .getattr("MappingProxyType")?
        .call1((from_json(py, &Value::Object(message.metadata.clone()))?,))?;
    let attachments = PyTuple::new(
        py,
        message
            .attachments
            .iter()
            .map(|a| to_attachment(py, a))
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(models
        .getattr("Message")?
        .call1((
            message.id.as_ref(),
            message.conversation_id.as_ref(),
            &message.channel,
            sender,
            &message.text,
            match message.role {
                Role::User => "user",
                Role::Assistant => "assistant",
            },
            metadata,
            &message.thread_id,
            message.addressed,
            attachments,
        ))?
        .unbind())
}

pub fn from_message(value: &Bound<'_, PyAny>) -> PyResult<Message> {
    let sender = value.getattr("sender")?;
    let attachments = value
        .getattr("attachments")?
        .try_iter()?
        .map(|item| from_attachment(&item?))
        .collect::<PyResult<Vec<_>>>()?;
    Ok(Message {
        id: value.getattr("id")?.extract::<String>()?.into(),
        conversation_id: value
            .getattr("conversation_id")?
            .extract::<String>()?
            .into(),
        channel: value.getattr("channel")?.extract()?,
        sender: Sender {
            id: sender.getattr("id")?.extract()?,
            display_name: sender.getattr("display_name")?.extract()?,
        },
        text: value.getattr("text")?.extract()?,
        role: match value.getattr("role")?.extract::<String>()?.as_str() {
            "user" => Role::User,
            "assistant" => Role::Assistant,
            _ => return Err(PyValueError::new_err("Unsupported message role")),
        },
        metadata: to_json(&value.getattr("metadata")?)?
            .as_object()
            .cloned()
            .unwrap_or_default(),
        thread_id: value.getattr("thread_id")?.extract()?,
        addressed: value.getattr("addressed")?.extract()?,
        attachments,
    })
}

pub fn from_rich_reply(value: &Bound<'_, PyAny>) -> PyResult<RichReply> {
    let blocks = to_json(&value.getattr("blocks")?)?;
    let attachments = to_json(&value.getattr("attachments")?)?;
    serde_json::from_value(serde_json::json!({"text":value.getattr("text")?.extract::<String>()?,"blocks":blocks,"attachments":attachments})).map_err(|error| PyValueError::new_err(error.to_string()))
}
