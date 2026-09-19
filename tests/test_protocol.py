import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_lsp
from lsprotocol import types
from pytest_lsp import ClientServerConfig, LanguageClient

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
REPO_ROOT = TESTS_DIR.parent.parent


def _capabilities() -> types.ClientCapabilities:
    return types.ClientCapabilities(
        general=types.GeneralClientCapabilities(position_encodings=["utf-16"]),
        text_document=types.TextDocumentClientCapabilities(
            hover=types.HoverClientCapabilities(
                content_format=[types.MarkupKind.Markdown]
            ),
            completion=types.CompletionClientCapabilities(
                completion_item=types.ClientCompletionItemOptions(
                    snippet_support=True
                )
            ),
            semantic_tokens=types.SemanticTokensClientCapabilities(
                requests=types.ClientSemanticTokensRequestOptions(full=True),
                token_types=[
                    "namespace",
                    "type",
                    "struct",
                    "enum",
                    "interface",
                    "typeParameter",
                    "parameter",
                    "variable",
                    "property",
                    "enumMember",
                    "function",
                    "method",
                    "keyword",
                    "comment",
                    "string",
                    "number",
                    "operator",
                ],
                token_modifiers=[
                    "declaration",
                    "definition",
                    "readonly",
                    "static",
                    "defaultLibrary",
                ],
                formats=[types.TokenFormat.Relative],
            ),
            definition=types.DefinitionClientCapabilities(link_support=False),
            references=types.ReferenceClientCapabilities(),
            rename=types.RenameClientCapabilities(),
        ),
    )


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "cwind_lsp", "--stdio"],
        server_env={
            **os.environ,
            "PYTHONPATH": str(SRC_DIR),
            "CWIND_HOME": str(REPO_ROOT),
            "PYTHONUTF8": "1",
        },
    ),
)
async def client(lsp_client: LanguageClient):
    await lsp_client.initialize_session(
        types.InitializeParams(
            capabilities=_capabilities(),
            initialization_options={
                "cwind": {"std_path": str(REPO_ROOT), "debounce_ms": 10}
            },
        )
    )
    yield
    await lsp_client.shutdown_session()


async def _request(client: LanguageClient, method: str, params, timeout: float = 30.0):
    return await asyncio.wait_for(
        client.protocol.send_request_async(method, params), timeout
    )


async def _wait_for_diagnostics(client: LanguageClient, uri: str, timeout: float = 30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while uri not in client.diagnostics and loop.time() < deadline:
        await asyncio.sleep(0.05)
    return client.diagnostics.get(uri, [])


async def test_hover_definition_and_semantics(client: LanguageClient, hello_world: Path):
    uri = hello_world.as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=uri, language_id="cwind", version=1, text=hello_world.read_text()
            )
        )
    )
    diagnostics = await _wait_for_diagnostics(client, uri)
    assert list(diagnostics) == []

    hover = await _request(
        client,
        "textDocument/hover",
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=0, character=3),
        ),
    )
    assert hover is not None
    assert "fn main" in hover.contents.value

    definition = await _request(
        client,
        "textDocument/definition",
        types.DefinitionParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=1, character=15),
        ),
    )
    assert definition
    assert "builtins" in definition[0].uri

    tokens = await _request(
        client,
        "textDocument/semanticTokens/full",
        types.SemanticTokensParams(
            text_document=types.TextDocumentIdentifier(uri=uri)
        ),
    )
    assert tokens is not None and tokens.data


async def test_prepare_and_rename(client: LanguageClient, hello_world: Path):
    uri = hello_world.as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=uri, language_id="cwind", version=1, text=hello_world.read_text()
            )
        )
    )
    await _wait_for_diagnostics(client, uri)
    prepared = await _request(
        client,
        "textDocument/prepareRename",
        types.PrepareRenameParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=0, character=3),
        ),
    )
    assert prepared is not None
    edit = await _request(
        client,
        "textDocument/rename",
        types.RenameParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=0, character=3),
            new_name="start",
        ),
    )
    assert edit is not None
    assert uri in edit.changes
    assert edit.changes[uri][0].new_text == "start"


async def test_completion_and_diagnostics_on_edit(client: LanguageClient, hello_world: Path):
    uri = hello_world.as_uri()
    source = hello_world.read_text()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=uri, language_id="cwind", version=1, text=source
            )
        )
    )
    await _wait_for_diagnostics(client, uri)
    edit = source.replace("builtins::print", "builtins::")
    client.text_document_did_change(
        types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=2),
            content_changes=[
                types.TextDocumentContentChangePartial(
                    range=types.Range(
                        start=types.Position(0, 0),
                        end=types.Position(100, 0),
                    ),
                    text=edit,
                )
            ],
        )
    )
    completion = await _request(
        client,
        "textDocument/completion",
        types.CompletionParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=1, character=14),
        ),
    )
    labels = {item.label for item in (completion.items or [])}
    assert "print" in labels

    client.text_document_did_change(
        types.DidChangeTextDocumentParams(
            text_document=types.VersionedTextDocumentIdentifier(uri=uri, version=3),
            content_changes=[
                types.TextDocumentContentChangePartial(
                    range=types.Range(
                        start=types.Position(0, 0), end=types.Position(100, 0)
                    ),
                    text='fn main() {\n    let x: Int = "hello";\n}\n',
                )
            ],
        )
    )
    diagnostics = await _wait_for_diagnostics(client, uri)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    while not diagnostics and loop.time() < deadline:
        await asyncio.sleep(0.05)
        diagnostics = client.diagnostics.get(uri, [])
    assert diagnostics
