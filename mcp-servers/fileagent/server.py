import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

import uvicorn
from docx import Document
from docx.shared import Pt as DocxPt
from mcp.server.fastmcp import FastMCP
from openpyxl import Workbook
from openpyxl.styles import Font
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape
import csv
import io
import json
import zipfile
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.responses import FileResponse, PlainTextResponse

MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8002"))
DOWNLOAD_PORT = int(os.getenv("DOWNLOAD_PORT", "8001"))
mcp = FastMCP(
    "fileagent",
    instructions=(
        "Choose the creation tool that matches the requested file format. "
        "For PowerPoint, use create_presentation, add_slide, add_text, and "
        "save_presentation. For PDF requests, call create_pdf directly and pass "
        "the requested page count in its pages argument. For Word or DOCX, use "
        "create_document. For Excel, XLSX, or spreadsheet requests, use "
        "create_spreadsheet. Do not create a PowerPoint when another format is requested."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
)

OUTPUT_DIR = Path("/tmp/fileagent")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_URL = os.getenv("FILEAGENT_PUBLIC_URL", "http://localhost:3080").rstrip("/")
LEGACY_PRESENTATION_NAME = "presentation.pptx"
TIMESTAMPED_ARTIFACT = re.compile(
    r"(?:presentation_\d{8}T\d{12}Z_[0-9a-f]{8}\.pptx|"
    r"pdf_\d{8}T\d{12}Z_[0-9a-f]{8}\.pdf|"
    r"document_\d{8}T\d{12}Z_[0-9a-f]{8}\.docx|"
    r"spreadsheet_\d{8}T\d{12}Z_[0-9a-f]{8}\.xlsx|"
    r"csv_\d{8}T\d{12}Z_[0-9a-f]{8}\.csv|"
    r"json_\d{8}T\d{12}Z_[0-9a-f]{8}\.json|"
    r"txt_\d{8}T\d{12}Z_[0-9a-f]{8}\.txt|"
    r"md_\d{8}T\d{12}Z_[0-9a-f]{8}\.md|"
    r"html_\d{8}T\d{12}Z_[0-9a-f]{8}\.html|"
    r"zip_\d{8}T\d{12}Z_[0-9a-f]{8}\.zip)"
)
MEDIA_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".zip": "application/zip",
}
# Logged at startup so a stale container image is obvious from the logs alone.
BUILD_ID = "multi-format-artifacts-v1"

ASSET_DIR = Path(__file__).parent / "assets"
BACKGROUND_PATH = ASSET_DIR / "akashx-background.png"
LOGO_SVG_PATH = ASSET_DIR / "akashx-logo.svg"
LOGO_PNG_PATH = ASSET_DIR / "akashx-logo.png"


@lru_cache(maxsize=1)
def _logo_png() -> bytes:
    """Render the canonical Logo.tsx SVG in a format supported by PowerPoint."""
    try:
        import cairosvg

        return cairosvg.svg2png(url=str(LOGO_SVG_PATH))
    except (ImportError, OSError):
        # Keep presentation creation available if Cairo is unavailable. The
        # bundled PNG is the same AkashX wordmark with a transparent background.
        return LOGO_PNG_PATH.read_bytes()


BODY_NAME = "AkashXBody"
BODY_LEFT = Inches(1)
BODY_TOP = Inches(2)
BODY_WIDTH = Inches(11.333)
BODY_HEIGHT = Inches(4.75)


def _apply_akashx_branding(prs: Presentation, slide) -> None:
    """Apply the AkashX background and logo to a slide before its content."""
    background = slide.shapes.add_picture(
        str(BACKGROUND_PATH),
        0,
        0,
        width=prs.slide_width,
        height=prs.slide_height,
    )
    # The source PNG has unused white margins on the right and bottom. Crop
    # them so its 16:9 artwork fills the slide instead of exposing white.
    background.crop_right = 1 / 6
    background.crop_bottom = 1 / 6

    slide.shapes.add_picture(
        BytesIO(_logo_png()),
        Inches(0.45),
        Inches(0.28),
        width=Inches(2.3),
    )


def _find_body_box(slide):
    """Return this slide's body textbox, or None when it has not been created yet."""
    for shape in slide.shapes:
        if shape.name == BODY_NAME:
            return shape
    return None


def _artifact_path(path: str) -> Path:
    """Resolve a URL or local path to a generated artifact in the output directory."""
    parsed = urlparse(path)
    output_dir = OUTPUT_DIR.resolve()
    if parsed.scheme:
        public_origin = urlparse(PUBLIC_URL)
        request_origin = (parsed.scheme, parsed.netloc)
        configured_origin = (public_origin.scheme, public_origin.netloc)
        if request_origin != configured_origin:
            raise ValueError("Unsupported presentation URL")
        filename = Path(parsed.path).name
        if parsed.path not in {
            f"/tmp/fileagent/{filename}",
            f"/files/{filename}",
        }:
            raise ValueError("Unsupported presentation URL")
        candidate = output_dir / filename
    else:
        candidate = Path(path)
        # IMPORTANT:
        # MCP tools sometimes return only the filename,
        # without the /tmp/fileagent directory.
        if not candidate.is_absolute():
            candidate = output_dir / candidate
    filename = candidate.name
    if (
        filename != LEGACY_PRESENTATION_NAME
        and TIMESTAMPED_ARTIFACT.fullmatch(filename) is None
    ):
        raise ValueError("Unsupported artifact filename")
    resolved_path = candidate.resolve()
    # Security: only allow files directly inside OUTPUT_DIR.
    if resolved_path.parent != output_dir:
        raise ValueError("Artifact path must be inside the output directory")
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Artifact does not exist: {filename}")
    return resolved_path


def _presentation_path(path: str) -> Path:
    """Resolve a generated PPTX, rejecting other supported artifact formats."""
    presentation_path = _artifact_path(path)
    if presentation_path.suffix != ".pptx":
        raise ValueError("Expected a PowerPoint presentation")
    return presentation_path


def _new_presentation_path() -> Path:
    return _new_artifact_path("presentation", ".pptx")


def _new_artifact_path(prefix: str, extension: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return OUTPUT_DIR / f"{prefix}_{timestamp}_{uuid4().hex[:8]}{extension}"


def _download_url(presentation_path: Path) -> str:
    return f"{PUBLIC_URL}/tmp/fileagent/{presentation_path.name}"


async def _send_artifact(request: Request) -> FileResponse | PlainTextResponse:
    filename = request.path_params["filename"]
    try:
        artifact_path = _artifact_path(str(OUTPUT_DIR / filename))
    except (FileNotFoundError, ValueError):
        return PlainTextResponse("File not found", status_code=404)
    return FileResponse(
        artifact_path,
        media_type=MEDIA_TYPES[artifact_path.suffix],
        filename=filename,
    )


async def download_file(request: Request) -> FileResponse | PlainTextResponse:
    return await _send_artifact(request)


download_app = Starlette(
    routes=[
        Route("/tmp/fileagent/{filename}", _send_artifact, methods=["GET"]),
        Route("/files/{filename}", download_file, methods=["GET"]),
    ]
)


@mcp.tool()
def create_presentation(title: str = "Presentation") -> str:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    # Title slide
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _apply_akashx_branding(prs, slide)
    left = Inches(1)
    top = Inches(2.5)
    width = Inches(11.333)
    height = Inches(1.5)
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(36)
    p.font.bold = True
    p.font.color.rgb = RGBColor(255, 255, 255)
    p.alignment = PP_ALIGN.CENTER
    presentation_path = _new_presentation_path()
    prs.save(presentation_path)
    return _download_url(presentation_path)

@mcp.tool()
def add_slide(path: str, title: str = "Slide") -> str:
    presentation_path = _presentation_path(path)
    prs = Presentation(presentation_path)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _apply_akashx_branding(prs, slide)
    left = Inches(0.5)
    top = Inches(1.15)
    width = Inches(12.333)
    height = Inches(1)
    txBox = slide.shapes.add_textbox(left, top, width, height)
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(28)
    p.font.bold = True
    p.font.color.rgb = RGBColor(255, 255, 255)
    prs.save(presentation_path)
    return _download_url(presentation_path)

@mcp.tool()
def add_text(path: str, text: str) -> str:
    """Append text to the current slide's body area.

    Repeated calls add new paragraphs to the same box, so content stacks
    downward instead of drawing overlapping text boxes on top of each other.
    """
    presentation_path = _presentation_path(path)
    prs = Presentation(presentation_path)
    slide = prs.slides[-1]
    body_box = _find_body_box(slide)
    if body_box is None:
        body_box = slide.shapes.add_textbox(
            BODY_LEFT, BODY_TOP, BODY_WIDTH, BODY_HEIGHT
        )
        body_box.name = BODY_NAME
    tf = body_box.text_frame
    tf.word_wrap = True
    # The first paragraph of a new textbox is already empty; reuse it for the
    # first insertion, then append for every later call.
    p = tf.paragraphs[0] if len(tf.paragraphs) == 1 and not tf.paragraphs[0].text else tf.add_paragraph()
    p.text = text
    p.font.size = Pt(18)
    p.font.color.rgb = RGBColor(255, 255, 255)
    prs.save(presentation_path)
    return _download_url(presentation_path)

@mcp.tool()
def save_presentation(path: str) -> str:
    presentation_path = _presentation_path(path)
    return _download_url(presentation_path)


@mcp.tool()
def create_pdf(title: str, content: str, pages: int = 0) -> str:
    """Create a downloadable PDF. Use pages for the requested page count (0 for automatic pagination)."""
    if pages < 0 or pages > 50:
        raise ValueError("pages must be between 0 and 50")
    pdf_path = _new_artifact_path("pdf", ".pdf")
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = colors.HexColor("#17324D")
    body_style = styles["BodyText"]
    body_style.fontName = "Helvetica"
    body_style.fontSize = 11
    body_style.leading = 16
    content_lines = content.splitlines()
    story = [Paragraph(escape(title), title_style), Spacer(1, 0.25 * inch)]
    if pages:
        for page in range(pages):
            start = page * len(content_lines) // pages
            end = (page + 1) * len(content_lines) // pages
            story.extend(
                Paragraph(escape(line) or " ", body_style)
                for line in content_lines[start:end]
            )
            if page < pages - 1:
                story.append(PageBreak())
    else:
        story.extend(
            Paragraph(escape(line) or " ", body_style)
            for line in content_lines
        )
    SimpleDocTemplate(str(pdf_path), pagesize=letter).build(story)
    return _download_url(pdf_path)


@mcp.tool()
def create_document(title: str, content: str) -> str:
    """Create a downloadable DOCX document; separate paragraphs with newlines."""
    document = Document()
    document.core_properties.title = title
    document.add_heading(title, level=0)
    for line in content.splitlines():
        paragraph = document.add_paragraph(line)
        paragraph.paragraph_format.space_after = DocxPt(8)
    document_path = _new_artifact_path("document", ".docx")
    document.save(document_path)
    return _download_url(document_path)


@mcp.tool()
def create_csv(title: str, rows: list[list[str]]) -> str:
    """Create a downloadable CSV file from a title and rows of cell values."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([title] + [f"col_{i}" for i in range(len(rows[0]))] if rows else [])
    for row in rows:
        writer.writerow(row)
    csv_path = _new_artifact_path("csv", ".csv")
    csv_path.write_text(output.getvalue())
    return _download_url(csv_path)


@mcp.tool()
def create_json(title: str, data: dict) -> str:
    """Create a downloadable JSON file from a title and a dict."""
    json_path = _new_artifact_path("json", ".json")
    json_path.write_text(json.dumps(data, indent=2))
    return _download_url(json_path)


@mcp.tool()
def create_text(title: str, content: str) -> str:
    """Create a downloadable TXT file from a title and plain-text content."""
    txt_path = _new_artifact_path("txt", ".txt")
    txt_path.write_text(content)
    return _download_url(txt_path)


@mcp.tool()
def create_markdown(title: str, content: str) -> str:
    """Create a downloadable MD file from a title and markdown content."""
    md_path = _new_artifact_path("md", ".md")
    md_path.write_text(content)
    return _download_url(md_path)


@mcp.tool()
def create_html(title: str, content: str) -> str:
    """Create a downloadable HTML file from a title and HTML content."""
    html_path = _new_artifact_path("html", ".html")
    html_path.write_text(content)
    return _download_url(html_path)


@mcp.tool()
def create_zip(title: str, files: dict[str, str]) -> str:
    """Create a downloadable ZIP archive from a dict of filename->content."""
    zip_path = _new_artifact_path("zip", ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, fcontent in files.items():
            zf.writestr(fname, fcontent)
    return _download_url(zip_path)


@mcp.tool()
def create_spreadsheet(
    title: str, rows: list[list[str]], sheet_name: str = "Sheet1"
) -> str:
    """Create an XLSX workbook from rows of cell values; the first row is the header."""
    workbook = Workbook()
    workbook.properties.title = title
    worksheet = workbook.active
    worksheet.title = re.sub(r"[\[\]:*?/\\]", "_", sheet_name)[:31] or "Sheet1"
    for row in rows:
        worksheet.append(row)
    if rows:
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
    spreadsheet_path = _new_artifact_path("spreadsheet", ".xlsx")
    workbook.save(spreadsheet_path)
    return _download_url(spreadsheet_path)


if __name__ == "__main__":
    print(
        f"[fileagent] {BUILD_ID} | mcp=http://{MCP_HOST}:{MCP_PORT}/mcp | "
        f"downloads={PUBLIC_URL}/tmp/fileagent/<name> | output={OUTPUT_DIR}",
        flush=True,
    )
    threading.Thread(
        target=uvicorn.run,
        args=(download_app,),
        kwargs={"host": MCP_HOST, "port": DOWNLOAD_PORT, "log_level": "warning"},
        daemon=True,
    ).start()
    mcp.run(transport="streamable-http")
