//! Rust Executor — V0.3 占位实现。
//!
//! 本二进制**不具备任何执行能力**，也不打算在本版本中具备。它存在的目的只有两个：
//!
//! 1. 让 `rust-executor` 成为一个可编译的 Cargo 包（此前 Cargo.toml 缺少 target，
//!    `cargo build` 直接以 "no targets specified in the manifest" 失败）；
//! 2. 把 ExecutionRequest / ExecutionResult 的**版本化协议形状**先固定下来，
//!    供 Python 侧后续对接。
//!
//! 任何请求都会被显式拒绝并以非零码退出。
//!
//! 安全提醒：不要因为这里叫 "Executor" 就以为它提供隔离。真正的隔离必须由
//! 容器 / gVisor / Firecracker 提供，Rust 进程本身不构成安全边界
//! （见 docs/security.md 与 docs/architecture.md）。

use std::io::{self, Read, Write};
use std::process::ExitCode;

use serde_json::{json, Value};

/// ExecutionRequest / ExecutionResult 的协议版本。
const SCHEMA_VERSION: &str = "1";

/// 单次请求的读取上限，避免被超大输入撑爆内存。
const MAX_REQUEST_BYTES: u64 = 1024 * 1024;

fn main() -> ExitCode {
    let mut raw = String::new();
    if let Err(err) = io::stdin().take(MAX_REQUEST_BYTES).read_to_string(&mut raw) {
        return reject("invalid_request", &format!("读取 stdin 失败: {err}"));
    }

    if raw.trim().is_empty() {
        return reject("invalid_request", "未在 stdin 收到 ExecutionRequest");
    }

    let request: Value = match serde_json::from_str(&raw) {
        Ok(value) => value,
        Err(err) => return reject("invalid_request", &format!("请求不是合法 JSON: {err}")),
    };

    let version = request
        .get("schema_version")
        .and_then(Value::as_str)
        .unwrap_or("");

    if version != SCHEMA_VERSION {
        return reject(
            "unsupported_schema_version",
            &format!("期望 schema_version \"{SCHEMA_VERSION}\"，实际收到 \"{version}\""),
        );
    }

    // 走到这里说明请求形状是对的，但功能尚未实现——明确拒绝，绝不静默降级。
    reject(
        "executor_not_implemented",
        "Rust Executor 是 V0.3 占位实现，拒绝一切代码执行请求；\
         隔离能力需由容器 / gVisor / Firecracker 提供，尚未接入",
    )
}

/// 输出结构化的 ExecutionResult 拒绝响应，并返回失败退出码。
fn reject(code: &str, message: &str) -> ExitCode {
    let result = json!({
        "schema_version": SCHEMA_VERSION,
        "status": "rejected",
        "error": { "code": code, "message": message },
    });

    let stdout = io::stdout();
    let mut out = stdout.lock();
    if serde_json::to_writer(&mut out, &result).is_ok() {
        let _ = writeln!(out);
    }

    ExitCode::from(1)
}
