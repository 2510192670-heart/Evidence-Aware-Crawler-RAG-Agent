"""Deterministic CSV and XLSX views over the existing result.json artifact."""
import csv
import io
import re
import zipfile
from xml.sax.saxutils import escape


def export_csv(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    names = list(rows[0]) if rows else []
    def safe(value):
        text = '' if value is None else str(value)
        if text.lstrip().startswith(('=', '+', '-', '@')) and isinstance(value, str):
            return "'" + text
        return text
    writer.writerow([safe(name) for name in names])
    writer.writerows([safe(row.get(name)) for name in names] for row in rows)
    return stream.getvalue().encode('utf-8-sig')


def export_xlsx(rows):
    names = list(rows[0]) if rows else []
    def column(index):
        label = ''
        while index:
            index, rem = divmod(index - 1, 26)
            label = chr(65 + rem) + label
        return label
    def cell(value, address):
        if value is None:
            return f'<c r="{address}"/>'
        if type(value) is bool:
            return f'<c r="{address}" t="b"><v>{int(value)}</v></c>'
        if type(value) in {int, float}:
            return f'<c r="{address}"><v>{value}</v></c>'
        # inlineStr cannot become a formula. Invalid XML control characters
        # have no representation in an Excel cell and are replaced explicitly.
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '\ufffd', str(value))
        return f'<c r="{address}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    data = [names] + [[row.get(name) for name in names] for row in rows]
    sheet_rows = ''.join(f'<row r="{i}">' + ''.join(cell(v, f'{column(j)}{i}') for j, v in enumerate(row, 1)) + '</row>'
                         for i, row in enumerate(data, 1))
    files = {
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + sheet_rows + '</sheetData></worksheet>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content).encode('utf-8'))
    return output.getvalue()
