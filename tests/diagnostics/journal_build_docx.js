// Turn report_render.json (written by journal_report.py) into a .docx.
//
//   node build_docx.js <report_render.json> <figures dir> <out.docx>
//
// Block types: title, h1, h2, p, figure, table. Paragraph text may carry
// *italic* spans (statistical symbols); nothing else is interpreted.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, ImageRun, Table, TableRow,
  TableCell, WidthType, AlignmentType, BorderStyle, ShadingType, VerticalAlign,
} = require("docx");

const [, , renderPath, figDir, outPath] = process.argv;
const blocks = JSON.parse(fs.readFileSync(renderPath, "utf8"));

const FONT = "Calibri";
const CONTENT_DXA = 9026;            // A4 with 1-inch margins
const CONTENT_IN = CONTENT_DXA / 1440;

function runs(text, extra = {}) {
  const out = [];
  for (const part of text.split(/(\*[^*]+\*)/)) {
    if (!part) continue;
    const italic = part.startsWith("*") && part.endsWith("*") && part.length > 2;
    out.push(new TextRun({ text: italic ? part.slice(1, -1) : part, italics: italic, font: FONT, ...extra }));
  }
  return out;
}

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { width: b.readUInt32BE(16), height: b.readUInt32BE(20) };
}

const rule = { style: BorderStyle.SINGLE, size: 6, color: "52514E" };
const hair = { style: BorderStyle.SINGLE, size: 2, color: "C3C2B7" };
const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };

function cell(text, width, { header = false, first = false, last = false, block = false } = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    verticalAlign: VerticalAlign.CENTER,
    margins: { top: 50, bottom: 50, left: 90, right: 90 },
    shading: header || block ? { type: ShadingType.CLEAR, color: "auto", fill: header ? "F0EFEC" : "F7F7F5" } : undefined,
    borders: {
      top: header ? rule : none,
      bottom: header ? rule : (last ? rule : hair),
      left: none, right: none,
    },
    children: [new Paragraph({
      alignment: first ? AlignmentType.LEFT : AlignmentType.CENTER,
      spacing: { before: 0, after: 0 },
      keepNext: !last,                  // keeps a short table on one page
      children: runs(String(text), { size: 18, bold: header || block }),
    })],
  });
}

function table(block) {
  const cols = block.header.length;
  const widths = block.widths && block.widths.length === cols
    ? block.widths.map((w) => Math.round((w / block.widths.reduce((a, b) => a + b, 0)) * CONTENT_DXA))
    : Array(cols).fill(Math.floor(CONTENT_DXA / cols));
  widths[widths.length - 1] += CONTENT_DXA - widths.reduce((a, b) => a + b, 0);
  const rows = [new TableRow({
    tableHeader: true,
    cantSplit: true,
    children: block.header.map((h, i) => cell(h, widths[i], { header: true, first: i === 0 })),
  })];
  block.rows.forEach((row, r) => {
    const isBlock = row.length === 1;                 // a section row spanning the table
    const last = r === block.rows.length - 1;
    if (isBlock) {
      rows.push(new TableRow({ children: [new TableCell({
        columnSpan: cols, width: { size: CONTENT_DXA, type: WidthType.DXA },
        margins: { top: 50, bottom: 50, left: 90, right: 90 },
        shading: { type: ShadingType.CLEAR, color: "auto", fill: "F7F7F5" },
        borders: { top: none, bottom: hair, left: none, right: none },
        children: [new Paragraph({ keepNext: true, spacing: { before: 0, after: 0 },
          children: runs(String(row[0]), { size: 18, bold: true }) })],
      })] }));
    } else {
      rows.push(new TableRow({
        cantSplit: true,
        children: row.map((v, i) => cell(v, widths[i], { first: i === 0, last })),
      }));
    }
  });
  return new Table({ width: { size: CONTENT_DXA, type: WidthType.DXA }, columnWidths: widths, rows });
}

const children = [];
for (const block of blocks) {
  if (block.type === "title") {
    children.push(new Paragraph({ heading: HeadingLevel.TITLE, children: runs(block.text) }));
  } else if (block.type === "h1") {
    children.push(new Paragraph({ heading: HeadingLevel.HEADING_1, children: runs(block.text) }));
  } else if (block.type === "h2") {
    children.push(new Paragraph({ heading: HeadingLevel.HEADING_2, children: runs(block.text) }));
  } else if (block.type === "p") {
    children.push(new Paragraph({ alignment: AlignmentType.JUSTIFIED, children: runs(block.text) }));
  } else if (block.type === "figure") {
    const file = path.join(figDir, block.file);
    const { width, height } = pngSize(file);
    const inches = Math.min(block.width_in || CONTENT_IN, CONTENT_IN);
    const w = Math.round(inches * 96);
    const h = Math.round((w * height) / width);
    children.push(new Paragraph({
      alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 200, after: 80 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(file),
        transformation: { width: w, height: h }, altText: { title: block.label, description: block.caption, name: block.file } })],
    }));
    children.push(new Paragraph({ style: "Caption", children: runs(block.caption) }));
  } else if (block.type === "table") {
    children.push(new Paragraph({ style: "Caption", keepNext: true, spacing: { before: 200, after: 80 }, children: runs(block.caption) }));
    children.push(table(block));
    if (block.note) {
      children.push(new Paragraph({ spacing: { before: 60, after: 200 }, children: runs(block.note, { size: 17, color: "52514E" }) }));
    } else {
      children.push(new Paragraph({ spacing: { before: 0, after: 120 }, children: [] }));
    }
  }
}

const doc = new Document({
  creator: "journal_report.py",
  title: blocks.find((b) => b.type === "title")?.text || "Results",
  styles: {
    default: { document: { run: { font: FONT, size: 22 }, paragraph: { spacing: { after: 140, line: 276 } } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 34, bold: true, color: "0B0B0B" }, paragraph: { spacing: { after: 240 } } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 28, bold: true, color: "0B0B0B" }, paragraph: { spacing: { before: 320, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 24, bold: true, color: "0B0B0B" }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
      { id: "Caption", name: "Caption", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 19, color: "3A3936" }, paragraph: { spacing: { before: 40, after: 220 } } },
    ],
  },
  sections: [{
    properties: { page: { margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outPath, buf);
  console.log(`wrote ${outPath} (${children.length} blocks)`);
});
