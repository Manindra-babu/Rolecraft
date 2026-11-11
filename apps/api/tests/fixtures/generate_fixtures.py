from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Pt, RGBColor
import fitz

OUT_DIR = Path(__file__).resolve().parent / "resumes"


def build_single_column_docx(path: Path) -> None:
    doc = Document()
    h = doc.add_paragraph()
    r = h.add_run("Priya Sharma")
    r.bold = True
    r.font.size = Pt(16)
    r.font.name = "Calibri"
    r.font.color.rgb = RGBColor(0x1F, 0x29, 0x33)

    doc.add_paragraph("priya.sharma@example.com | +91 98765 43210 | Bengaluru, India | github.com/priyasharma")

    doc.add_paragraph("Summary").runs[0].bold = True
    doc.add_paragraph("Final-year Computer Science undergraduate building backend APIs with Python, FastAPI, and PostgreSQL.")

    doc.add_paragraph("Experience").runs[0].bold = True
    doc.add_paragraph("Software Engineering Intern | CloudScale Systems | May 2025 - Aug 2025")
    b1 = doc.add_paragraph("Built FastAPI microservices for invoice processing, cutting response latency by 28%.", style="List Bullet")
    b1.runs[0].font.name = "Calibri"
    b2 = doc.add_paragraph("Wrote pytest integration tests covering 18 REST endpoints and PostgreSQL queries.", style="List Bullet")
    b2.runs[0].font.name = "Calibri"

    doc.add_paragraph("Projects").runs[0].bold = True
    doc.add_paragraph("TaskFlow Queue | Python, Redis, Docker | github.com/priyasharma/taskflow")
    doc.add_paragraph("Implemented a distributed job queue with exponential backoff retries.", style="List Bullet")

    doc.add_paragraph("Education").runs[0].bold = True
    doc.add_paragraph("B.Tech in Computer Science | NIT Karnataka | 2022 - 2026")

    doc.add_paragraph("Skills").runs[0].bold = True
    doc.add_paragraph("Languages & Frameworks: Python, FastAPI, TypeScript, SQL, PostgreSQL, Redis, Docker, Git")

    doc.add_paragraph("Certifications").runs[0].bold = True
    doc.add_paragraph("AWS Certified Cloud Practitioner")

    doc.save(str(path))


def build_two_column_docx(path: Path) -> None:
    doc = Document()
    sectPr = doc.sections[0]._sectPr
    cols = sectPr.find(qn("w:cols"))
    if cols is None:
        cols = OxmlElement("w:cols")
        sectPr.append(cols)
    cols.set(qn("w:num"), "2")
    cols.set(qn("w:space"), "720")

    doc.add_paragraph("Rohan Verma")
    doc.add_paragraph("rohan.verma@example.com | Austin, TX | linkedin.com/in/rohanverma")
    doc.add_paragraph("Experience")
    doc.add_paragraph("Backend Intern | DataBridge | Jun 2025 - Sep 2025")
    doc.add_paragraph("• Automated ETL pipelines in Python and SQL for daily analytics.")
    doc.add_paragraph("Skills")
    doc.add_paragraph("Python, PostgreSQL, Docker, Linux")
    doc.save(str(path))


def build_table_based_docx(path: Path) -> None:
    doc = Document()
    table = doc.add_table(rows=1, cols=2)
    left_cell = table.cell(0, 0)
    right_cell = table.cell(0, 1)

    left_cell.text = "Ananya Patel\nananya@example.com\nSkills\nPython, React, TypeScript, FastAPI\nEducation\nB.S. Computer Science | UC Davis | 2021 - 2025"
    p_right = right_cell.paragraphs[0]
    p_right.text = "Experience"
    right_cell.add_paragraph("Full Stack Intern | FinLedger | Jan 2025 - May 2025")
    right_cell.add_paragraph("• Built TypeScript dashboard widgets backed by FastAPI REST endpoints.")
    right_cell.add_paragraph("Projects")
    right_cell.add_paragraph("BudgetPulse | React, FastAPI, SQLite")
    right_cell.add_paragraph("• Created CSV bank statement parser with unit tests.")

    doc.save(str(path))


def build_textbox_docx(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("Experience")
    doc.add_paragraph("ML Research Assistant | VisionLab | 2024 - 2025")
    doc.add_paragraph("• Trained image classifiers in PyTorch and logged metrics.")

    # Inject a DrawingML/VML text box into a paragraph XML
    p = doc.add_paragraph()
    txbx_xml = f"""
    <w:r {nsdecls('w')} xmlns:v="urn:schemas-microsoft-com:vml">
      <w:pict>
        <v:shape style="width:200pt;height:40pt">
          <v:textbox>
            <w:txbxContent>
              <w:p><w:r><w:t>Karan Kapoor</w:t></w:r></w:p>
              <w:p><w:r><w:t>karan.kapoor@example.com</w:t></w:r></w:p>
            </w:txbxContent>
          </v:textbox>
        </v:shape>
      </w:pict>
    </w:r>
    """
    p._p.append(parse_xml(txbx_xml))
    doc.save(str(path))


def build_sample_pdf(path: Path) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    text = (
        "Maya Nair\n"
        "maya.nair@example.com | Seattle, WA | github.com/mayanair\n\n"
        "Summary\n"
        "Graduate software engineer focused on backend systems and search.\n\n"
        "Experience\n"
        "Software Engineer Intern | SearchCore | Jun 2025 - Sep 2025\n"
        "• Indexed 2M documents with Python and PostgreSQL full-text search.\n\n"
        "Skills\n"
        "Python, PostgreSQL, FastAPI, Git\n"
    )
    page.insert_text((50, 60), text, fontsize=11)
    pdf.save(str(path))
    pdf.close()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    build_single_column_docx(OUT_DIR / "sample_single_column.docx")
    build_two_column_docx(OUT_DIR / "sample_two_column.docx")
    build_table_based_docx(OUT_DIR / "sample_table_based.docx")
    build_textbox_docx(OUT_DIR / "sample_textbox.docx")
    build_sample_pdf(OUT_DIR / "sample_resume.pdf")
    print("Generated all resume fixtures in", OUT_DIR)


if __name__ == "__main__":
    main()
