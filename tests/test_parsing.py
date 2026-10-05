import base64

from src.connector import parse_message, sanitize_filename


def test_parse_simple_text_message():
    raw = (
        b"From: a@b.fr\r\n"
        b"To: c@d.fr\r\n"
        b"Subject: Hello\r\n"
        b"Date: Mon, 05 Oct 2026 10:00:00 +0200\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Bonjour, ceci est le corps.\r\n"
    )
    msg = parse_message(raw)
    assert msg.subject == "Hello"
    assert "ceci est le corps" in msg.body_text
    assert msg.body_html == ""
    assert msg.attachments == []
    assert msg.from_ == "a@b.fr"


def test_parse_non_ascii_subject():
    raw = (
        b"From: a@b.fr\r\n"
        b"Subject: =?utf-8?Q?Caf=C3=A9_?= OK\r\n"
        b"Date: Mon, 05 Oct 2026 10:00:00 +0200\r\n"
        b"\r\n"
        b"body\r\n"
    )
    msg = parse_message(raw)
    assert msg.subject == "Café OK"


def test_parse_multipart_with_attachment():
    pdf_bytes = b"%PDF-1.4 tiny fake pdf"
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    raw = (
        b"From: a@b.fr\r\n"
        b"To: c@d.fr\r\n"
        b"Subject: Rapport\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"Texte =C3=A0 l'=C3=89tude\r\n"
        b"--BOUND\r\n"
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="rapport.pdf"\r\n'
        b"\r\n"
        + b64.encode("ascii")
        + b"\r\n"
        b"--BOUND--\r\n"
    )
    msg = parse_message(raw, uid="42")
    assert msg.uid == "42"
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.filename == "rapport.pdf"
    assert att.size == len(pdf_bytes)
    assert att.content_type == "application/pdf"
    assert "Texte à l'Étude" in msg.body_text


def test_parse_html_only_message():
    raw = (
        b"From: a@b.fr\r\n"
        b"Subject: News\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><p>Visible text here</p></body></html>\r\n"
    )
    msg = parse_message(raw)
    assert msg.body_html != ""
    assert "Visible text here" in msg.body_text
    assert "<p>" not in msg.body_text


def test_parse_empty_body():
    raw = b"From: a@b.fr\r\nSubject: Empty\r\n\r\n"
    msg = parse_message(raw)
    assert msg.body_text == ""
    assert msg.attachments == []


def test_sanitize_filename_rejects_traversal():
    result = sanitize_filename("../../etc/cron.d/evil")
    assert result != ""
    assert "/" not in result
    assert "\\" not in result
    assert ".." not in result


def test_sanitize_filename_keeps_unicode_and_ext():
    assert sanitize_filename("café-2026.pdf") == "café-2026.pdf"
