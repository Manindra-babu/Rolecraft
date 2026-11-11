# Supported Resume Layouts

ResumeIQ preserves your original `.docx` formatting by editing paragraphs and runs in place rather than replacing your resume with a generic template.

| Layout Type | Support Level | Behavior |
| --- | --- | --- |
| **Single-column DOCX** | **Full** | Paragraphs, headings, bullet lists (`numPr`), font family/size/color, and spacing are preserved in place. New bullets and projects clone the formatting of an existing bullet in the target section. |
| **Table-based DOCX** | **Full / Good** | Cells inside `<w:tbl>` structures (such as two-column tables or sidebar layouts) are indexed by `(table_index, row_index, col_index, paragraph_index)` and edited in place while keeping column widths and borders intact. |
| **Two-column section DOCX** (`<w:cols w:num="2"/>`) | **Partial** | Text inside body paragraphs is edited in place with full run styling, though adding long new projects may shift column breaks. Always verify the rendered PDF preview and page-count check. |
| **Text-box / floating-shape DOCX** (`<w:txbxContent>`) | **Partial** | Text boxes inside DrawingML / VML shapes are extracted for analysis; in-place editing prioritizes standard body and table paragraphs. Elements inside text boxes are flagged if an automated edit cannot safely target them. |
| **PDF Upload** | **Analysis Only** | Text and sections are extracted via `PyMuPDF` / `pdfplumber`. Because PDFs store absolute glyphs rather than editable paragraphs, exact-format editing is unavailable; ResumeIQ offers a clean, ATS-friendly DOCX export approximating the extracted structure. |
