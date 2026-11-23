"""GitHub Actions logging, outputs, and step summary helpers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager


def _escape(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def log(message: str) -> None:
    print(message, flush=True)


def warning(message: str) -> None:
    print(f"::warning::{_escape(message)}", flush=True)


def error(message: str) -> None:
    print(f"::error::{_escape(message)}", flush=True)


@contextmanager
def log_group(title: str) -> Iterator[None]:
    print(f"::group::{_escape(title)}", flush=True)
    try:
        yield
    finally:
        print("::endgroup::", flush=True)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        if "\n" in value:
            delimiter = f"ghadelimiter_{uuid.uuid4().hex}"
            handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            handle.write(f"{name}={value}\n")


def add_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(markdown.rstrip() + "\n")
