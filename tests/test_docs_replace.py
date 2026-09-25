from unittest.mock import MagicMock, patch

from google_services import docs


def _run(end_index: int, text: str):
    service = MagicMock()
    with patch.object(docs, "docs_service", return_value=service), \
         patch.object(docs, "guard_not_forbidden"), \
         patch.object(docs, "_end_index", return_value=end_index):
        docs.replace_full_text("doc123", text)
    return service


def _requests(service):
    return service.documents().batchUpdate.call_args.kwargs["body"]["requests"]


def test_empty_doc_skips_delete():
    reqs = _requests(_run(2, "hello\n"))
    assert [list(r)[0] for r in reqs] == ["insertText"]


def test_non_empty_doc_deletes_then_inserts():
    reqs = _requests(_run(10, "hello\n"))
    assert [list(r)[0] for r in reqs] == ["deleteContentRange", "insertText"]
    assert reqs[0]["deleteContentRange"]["range"] == {"startIndex": 1, "endIndex": 9}


def test_empty_doc_empty_text_no_call():
    service = _run(2, "")
    service.documents().batchUpdate.assert_not_called()
