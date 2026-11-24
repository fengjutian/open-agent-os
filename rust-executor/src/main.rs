//! Rust Executor —— V0.3 Docker 执行后端。
//!
//! 从 stdin 读一个 JSON `ExecutionRequest`，交给 Docker 隔离执行，向 stdout 写一个
//! JSON `ExecutionResult`。协议版本固定在 `schema_version = "1"`。
//!
//! 退出码约定：
//!
//! - `0`：产出了合法结果（即使工具本身失败/超时——那属于结果，不是执行器故障）；
//! - `1`：请求无法解析或校验失败（stdout 里仍会有一条 `rejected` 结果）。
//!
//! 安全边界由 Docker 提供（见 `docker.rs`）；本进程不直接执行用户命令。

mod artifacts;
mod docker;
mod protocol;

use std::io::{self, Read, Write};
use std::process::ExitCode;

use serde_json::Value;

use protocol::{ExecutionRequest, ExecutionResult, MAX_REQUEST_BYTES, SCHEMA_VERSION};

#[tokio::main]
async fn main() -> ExitCode {
    if std::env::args().any(|arg| arg == "--check") {
        return check();
    }

    let mut raw = String::new();
    if let Err(err) = io::stdin()
        .take(MAX_REQUEST_BYTES)
        .read_to_string(&mut raw)
    {
        return emit_rejected("invalid_request", format!("读取 stdin 失败: {err}"));
    }
    if raw.trim().is_empty() {
        return emit_rejected("invalid_request", "未在 stdin 收到 ExecutionRequest");
    }

    let value: Value = match serde_json::from_str(&raw) {
        Ok(value) => value,
        Err(err) => return emit_rejected("invalid_request", format!("请求不是合法 JSON: {err}")),
    };

    let version = value
        .get("schema_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    if version != SCHEMA_VERSION {
        return emit_rejected(
            "unsupported_schema_version",
            format!("期望 schema_version \"{SCHEMA_VERSION}\"，实际收到 \"{version}\""),
        );
    }

    let request: ExecutionRequest = match serde_json::from_value(value) {
        Ok(request) => request,
        Err(err) => return emit_rejected("invalid_request", format!("请求字段不合法: {err}")),
    };

    let job = match request.into_job() {
        Ok(job) => job,
        Err(err) => return emit_rejected("invalid_request", err.0),
    };

    let name = docker::container_name();
    let result = docker::run(&job, &name).await;
    emit(&result, ExitCode::SUCCESS)
}

/// `agent-executor --check`：给运维一个不执行任何东西的健康检查。
fn check() -> ExitCode {
    let available = docker::docker_available();
    let payload = serde_json::json!({
        "schema_version": SCHEMA_VERSION,
        "docker_available": available,
        "default_image": std::env::var("AGENT_EXECUTOR_DEFAULT_IMAGE")
            .unwrap_or_else(|_| "python:3.12-slim".to_string()),
        "default_user": protocol::default_user(),
    });

    let stdout = io::stdout();
    let mut out = stdout.lock();
    if serde_json::to_writer(&mut out, &payload).is_ok() {
        let _ = writeln!(out);
        let _ = out.flush();
    }

    if available {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn emit_rejected(code: &str, message: impl Into<String>) -> ExitCode {
    emit(&ExecutionResult::rejected(code, message), ExitCode::from(1))
}

fn emit(result: &ExecutionResult, code: ExitCode) -> ExitCode {
    let stdout = io::stdout();
    let mut out = stdout.lock();
    if serde_json::to_writer(&mut out, result).is_err() {
        return ExitCode::from(1);
    }
    let _ = writeln!(out);
    let _ = out.flush();
    code
}
