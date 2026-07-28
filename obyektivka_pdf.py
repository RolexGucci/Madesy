"""
Obyektivka (MA'LUMOTNOMA) PDF generator.
LibreOffice shart emas - to'g'ridan-to'g'ri reportlab orqali PDF yasaydi.
Bu yengil, tez va bepul serverlarda ham ishonchli ishlaydi.
"""
import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

_FONTS_REGISTERED = False


def _register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont("Serif", os.path.join(FONTS_DIR, "LiberationSerif-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Serif-Bold", os.path.join(FONTS_DIR, "LiberationSerif-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Serif-Italic", os.path.join(FONTS_DIR, "LiberationSerif-Italic.ttf")))
    _FONTS_REGISTERED = True


def _styles():
    return {
        "title": ParagraphStyle("title", fontName="Serif-Bold", fontSize=13, alignment=1, spaceAfter=4),
        "name": ParagraphStyle("name", fontName="Serif-Bold", fontSize=12, alignment=1, spaceAfter=10),
        "normal": ParagraphStyle("normal", fontName="Serif", fontSize=10.5, leading=14),
        "bold": ParagraphStyle("bold", fontName="Serif-Bold", fontSize=10.5, leading=14),
        "heading": ParagraphStyle("heading", fontName="Serif-Bold", fontSize=12, alignment=1, spaceBefore=8, spaceAfter=6),
        "small_bold": ParagraphStyle("small_bold", fontName="Serif-Bold", fontSize=9, leading=11),
        "small": ParagraphStyle("small", fontName="Serif", fontSize=9, leading=11),
    }


def _kv(label, value, styles):
    """'Label: qiymat' formatidagi paragraf - bold label + oddiy qiymat."""
    return Paragraph(f'<font name="Serif-Bold">{label}</font> {value or ""}', styles["normal"])


def build_obyektivka_pdf(data: dict, photo_path: str = None) -> io.BytesIO:
    """DOCX generatoriga o'xshash - bir xil ma'lumot tuzilmasini kutadi."""
    _register_fonts()
    styles = _styles()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=2 * cm, bottomMargin=2 * cm,
        leftMargin=2 * cm, rightMargin=2 * cm,
    )

    story = []
    story.append(Paragraph("MA'LUMOTNOMA", styles["title"]))
    story.append(Paragraph(data.get("fio", ""), styles["name"]))

    left_content = [
        Paragraph(data.get("sarlavha_yil", ""), styles["normal"]),
        Paragraph(f'<font name="Serif-Bold">{data.get("tashkilot", "")}</font>', styles["normal"]),
    ]
    if photo_path and os.path.exists(photo_path):
        try:
            img = Image(photo_path, width=2.7 * cm, height=3.6 * cm)
            photo_table = Table([[img]], colWidths=[2.9 * cm], rowHeights=[3.8 * cm])
            photo_table.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 1, colors.black),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            header_table = Table([[left_content, photo_table]], colWidths=[12.5 * cm, 3.5 * cm])
        except Exception:
            header_table = Table([[left_content]], colWidths=[16 * cm])
    else:
        header_table = Table([[left_content]], colWidths=[16 * cm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))

    def two_col(label1, val1, label2, val2):
        t = Table(
            [[_kv(label1, val1, styles), _kv(label2, val2, styles)]],
            colWidths=[8 * cm, 8 * cm],
        )
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    story.append(two_col("Tug'ilgan yili:", data.get("tug_yil"), "Tug'ilgan joyi:", data.get("tug_joy")))
    story.append(two_col("Millati:", data.get("millat"), "Partiyaviyligi:", data.get("partiya")))
    story.append(two_col("Ma'lumoti:", data.get("malumot"), "Tamomlagan:", data.get("tamomlagan")))
    story.append(_kv("Ma'lumoti bo'yicha mutaxassisligi:", data.get("mutaxassislik"), styles))
    story.append(Spacer(1, 4))
    story.append(two_col("Ilmiy darajasi:", data.get("ilmiy_daraja"), "Ilmiy unvoni:", data.get("ilmiy_unvon")))
    story.append(two_col("Qaysi chet tillarini biladi:", data.get("chet_til"), "Harbiy (maxsus) unvoni:", data.get("harbiy")))

    story.append(Spacer(1, 4))
    story.append(_kv("Davlat mukofotlari bilan taqdirlanganmi (qanaqa):", "", styles))
    story.append(Paragraph(data.get("mukofot", ""), styles["normal"]))
    story.append(Spacer(1, 4))
    story.append(_kv(
        "Xalq deputatlari, respublika, viloyat, shahar va tuman Kengashi deputatimi "
        "yoki boshqa saylanadigan organlarning a'zosimi (to'liq ko'rsatilishi lozim):", "", styles
    ))
    story.append(Paragraph(data.get("deputat", ""), styles["normal"]))

    if data.get("jshshir"):
        story.append(Spacer(1, 4))
        story.append(_kv("JSHSHIR raqami:", data.get("jshshir"), styles))

    story.append(Spacer(1, 6))
    story.append(Paragraph("MEHNAT FAOLIYATI", styles["heading"]))
    for item in data.get("mehnat", []):
        story.append(Paragraph(f"{item.get('yillar','')} yy. - {item.get('tashkilot','')}", styles["normal"]))

    if data.get("tel"):
        story.append(Spacer(1, 4))
        story.append(_kv("Tel raqami:", data.get("tel"), styles))

    story.append(Spacer(1, 10))
    story.append(Paragraph(f"{data.get('fio','')}ning yaqin qarindoshlari haqida", styles["heading"]))
    story.append(Paragraph("MA'LUMOT", styles["heading"]))

    headers = ["Qarindosh-ligi", "Familiyasi, ismi va\notasining ismi",
               "Tug'ilgan yili\nva joyi", "Ish joyi va\nlavozimi", "Turar joyi"]
    table_data = [[Paragraph(h.replace("\n", "<br/>"), styles["small_bold"]) for h in headers]]

    for q in data.get("qarindoshlar", []):
        row = [
            Paragraph(f'<font name="Serif-Bold">{q.get("munosabat","")}</font>', styles["small"]),
            Paragraph(q.get("fio", ""), styles["small"]),
            Paragraph(q.get("tug", ""), styles["small"]),
            Paragraph(q.get("ish", ""), styles["small"]),
            Paragraph(q.get("turar", ""), styles["small"]),
        ]
        table_data.append(row)

    col_widths = [2.2 * cm, 3.8 * cm, 3.1 * cm, 3.3 * cm, 3.6 * cm]
    qar_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    qar_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(qar_table)

    if data.get("pasport"):
        story.append(Spacer(1, 10))
        story.append(_kv("Pasport seriya va raqami:", data.get("pasport"), styles))

    doc.build(story)
    buf.seek(0)
    return buf
