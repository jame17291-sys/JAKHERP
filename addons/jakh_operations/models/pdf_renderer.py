"""Offline, sandboxed Chromium fallback for governed JAKH reports only.

Odoo 20 resolves an engine before calling _run_wkhtmltopdf(self, args).
Use the current engine interface; do not replace native invoice rendering.
"""
import copy
import base64
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import socket
from urllib.parse import urlencode

from lxml import etree, html as lxml_html

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.pdf import PdfReader, PdfWriter, PdfReadError


JAKH_REPORTS = frozenset({
    'jakh_operations.construction_control_document',
    'jakh_operations.project_cash_forecast_document',
    'jakh_operations.financial_control_document',
})
ENGINE = 'jakh_chromium'
MAX_HTML = 16 * 1024 * 1024
MAX_PDF = 50 * 1024 * 1024
TIMEOUT_SECONDS = 45
CLASS = "contains(concat(' ', normalize-space(@class), ' '), ' %s ')"
CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; object-src 'none'; "
       "frame-src 'none'; base-uri 'none'; form-action 'none'; "
       "img-src data:; font-src data:; style-src 'unsafe-inline' data:")


def _chromium_binary():
    configured = os.environ.get('JAKH_PDF_CHROMIUM_BINARY', '').strip()
    candidates = [configured] if configured else [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        shutil.which('chromium') or '', shutil.which('chromium-browser') or '',
        shutil.which('google-chrome') or '',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_absolute() and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def _paper_spec(paper, landscape=False):
    # Millimetres are the native paperformat unit; custom dimensions are validated.
    formats = {'A3': (297, 420), 'A4': (210, 297), 'A5': (148, 210),
               'Letter': (215.9, 279.4), 'Legal': (215.9, 355.6)}
    width, height = formats.get(paper.format, (paper.page_width, paper.page_height))
    if not width or not height or not 50 <= width <= 1000 or not 50 <= height <= 1000:
        raise ValueError('unsupported-paper')
    if landscape or paper.orientation == 'Landscape':
        width, height = max(width, height), min(width, height)
    margins = [float(paper.margin_top), float(paper.margin_right), float(paper.margin_bottom), float(paper.margin_left)]
    if any(not 0 <= value <= 100 for value in margins) or width <= margins[1] + margins[3] or height <= margins[0] + margins[2]:
        raise ValueError('unsupported-margins')
    return {'width': width, 'height': height, 'top': margins[0], 'right': margins[1],
            'bottom': margins[2], 'left': margins[3]}


def _offline_documents(source, paper, primary='#176f67'):
    if isinstance(source, str):
        source = source.encode('utf-8')
    if not source or len(source) > MAX_HTML:
        raise ValueError('html-size')
    root = lxml_html.document_fromstring(source, parser=lxml_html.HTMLParser(encoding='utf-8', no_network=True))
    # CSP is defence in depth: remove active content and every external asset.
    for node in root.xpath('//script|//link|//iframe|//frame|//object|//embed|//base|//form|//meta[@http-equiv]'):
        node.getparent().remove(node)
    for node in list(root.iter()):
        local = str(node.tag).split('}')[-1].split(':')[-1].lower()
        if local in ('script', 'iframe', 'frame', 'object', 'embed', 'base') or (
            local == 'style' and node.getparent() is not None and node.getparent().tag != 'head'
        ):
            if node.getparent() is not None:
                node.getparent().remove(node)
    for node in root.iter():
        for name, value in list(node.attrib.items()):
            if name.lower().startswith('on') or name in ('srcset', 'action', 'formaction', 'poster', 'background'):
                del node.attrib[name]
            elif name == 'style':
                # Header/footer templates have their own isolated rendering context.
                # Preserve simple presentation, never CSS resource or escaped URLs.
                safe = []
                for declaration in value.split(';'):
                    if ':' not in declaration:
                        continue
                    key, content = declaration.split(':', 1)
                    if re.fullmatch(r'[a-zA-Z-]+', key.strip()) and not any(char in content for char in '()\\@<>'):
                        safe.append(key.strip() + ':' + content.strip())
                node.attrib[name] = ';'.join(safe)
            elif name in ('src', 'href', 'xlink:href', '{http://www.w3.org/1999/xlink}href'):
                if node.tag == 'a' and re.match(r'^(https?://|#)', value, re.I):
                    continue
                if name == 'src' and node.tag == 'img' and re.match(r'^data:image/(png|jpeg|gif|webp|svg\+xml);base64,', value, re.I):
                    continue
                if value.startswith('#'):
                    continue
                del node.attrib[name]
            elif re.search(r'url\s*\(', value, re.I) and not re.fullmatch(r'url\(#[a-zA-Z0-9_-]+\)', value):
                del node.attrib[name]
    articles = root.xpath('//*[%s and (@data-oe-id or @data-oe-model)]' % (CLASS % 'article'))
    if not articles:
        articles = root.xpath('//*[%s]' % (CLASS % 'article'))
    if not articles:
        articles = [root.find('body')]
    if len(articles) > 20:
        raise ValueError('too-many-report-documents')
    documents, ids = [], []
    primary = primary if re.fullmatch(r'#[0-9a-fA-F]{6}', primary or '') else '#176f67'
    for article in articles:
        if article is None:
            raise ValueError('missing-body')
        raw_id = article.get('data-oe-id')
        ids.append(int(raw_id) if raw_id and raw_id.isdigit() else None)
        header = footer = None
        if article is not root.find('body'):
            previous = article.getprevious()
            while previous is not None:
                if 'article' in previous.get('class', '').split():
                    break
                if 'header' in previous.get('class', '').split():
                    header = copy.deepcopy(previous); break
                previous = previous.getprevious()
            following = article.getnext()
            while following is not None:
                if 'article' in following.get('class', '').split():
                    break
                if 'footer' in following.get('class', '').split():
                    footer = copy.deepcopy(following); break
                following = following.getnext()
        # Do not silently clip oversized custom legal footers in this fallback.
        if footer is not None and len(footer.text_content()) > 1600:
            raise ValueError('oversized-footer')
        if (header is not None and paper['top'] < 20) or (footer is not None and paper['bottom'] < 12):
            raise ValueError('insufficient-header-footer-margin')
        rtl = article.get('dir') == 'rtl' or bool(article.xpath('.//*[@dir="rtl"]'))
        document = etree.Element('html', lang='ar' if rtl else 'en', dir='rtl' if rtl else 'ltr', attrib={'data-jakh-paper': json.dumps(paper)})
        head = etree.SubElement(document, 'head')
        etree.SubElement(head, 'meta', charset='utf-8')
        etree.SubElement(head, 'meta', {'http-equiv': 'Content-Security-Policy', 'content': CSP})
        etree.SubElement(head, 'title').text = 'JAKH ERP report'
        # Keep resource-free native inline colours, never external CSS or font URLs.
        for style in root.xpath('//head/style'):
            if not any(value in (style.text or '').lower() for value in ('url', '@import', '\\')):
                head.append(copy.deepcopy(style))
        css = '''
@page { size:%(width)smm %(height)smm; margin:%(top)smm %(right)smm %(bottom)smm %(left)smm; }
* { box-sizing:border-box; } html,body { margin:0; padding:0; }
body { font:11px Arial,"Noto Sans Arabic","Geeza Pro",sans-serif; color:#20333b; direction:%(direction)s; text-align:start; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
.header { font-size:10px; } .footer { font-size:8px; }
.header img { max-width:40mm; max-height:17mm; object-fit:contain; } .footer img { max-height:6mm; }
.footer .page,.footer .topage { display:none; } .article { padding:0!important; margin:0!important; }
div.page { padding:0!important; margin:0!important; break-after:page; } div.page:last-child { break-after:auto; }
h1,h2,h3 { color:%(primary)s; break-after:avoid; } h1 { font-size:20px; } h2 { font-size:17px; margin:0 0 8px; } h3 { font-size:13px; }
p { margin:0 0 7px; } ul { margin:0; padding-inline-start:18px; } .list-unstyled { list-style:none; padding:0; }
table { width:100%%; border-collapse:collapse; } thead { display:table-header-group; } tfoot { display:table-footer-group; }
th,td { padding:5px; border-bottom:1px solid #dbe5e3; text-align:start; vertical-align:top; word-break:normal; overflow-wrap:break-word; }
th { background:#eef5f3; color:#174b45; font-weight:600; } tr { break-inside:avoid; }
.table-borderless td,.table-borderless th { border:0; background:transparent; } .table-sm td,.table-sm th { padding:4px; }
img { max-width:100%%; } .row,.d-flex { display:flex; gap:8px; } .col-6 { width:50%%; } .col-auto { flex:0 0 auto; } .w-100 { width:100%%; }
.text-center { text-align:center; } .text-end { text-align:end; } .text-start { text-align:start; }
.align-top { vertical-align:top; } .border-top { border-top:1px solid #dbe5e3; } .pt-2 { padding-top:6px; }
.text-muted,.small { color:#52646a; font-size:9px; } .fw-bold { font-weight:bold; }
''' % {**paper, 'direction': 'rtl' if rtl else 'ltr', 'primary': primary}
        etree.SubElement(head, 'style').text = css
        body = etree.SubElement(document, 'body')
        if header is not None:
            body.append(header)
        if article.tag == 'body':
            for child in article:
                body.append(copy.deepcopy(child))
        else:
            body.append(copy.deepcopy(article))
        if footer is not None:
            body.append(footer)
        documents.append(etree.tostring(document, encoding='utf-8', method='html', doctype='<!doctype html>'))
    return documents, ids


def _validate_pdf(data):
    if not data or not data.startswith(b'%PDF-') or len(data) > MAX_PDF:
        raise ValueError('invalid-pdf')
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except PdfReadError as exc:
        raise ValueError('invalid-pdf-structure') from exc
    if not 1 <= len(reader.pages) <= 500:
        raise ValueError('invalid-page-count')
    return reader


def _jakh_pdf_bytes(parts):
    writer = PdfWriter()
    for part in parts:
        for page in _validate_pdf(part).pages:
            writer.add_page(page)
    writer.add_metadata({'/Title': 'JAKH ERP report', '/Author': 'JAKH ERP',
                         '/Creator': 'JAKH ERP', '/Producer': 'JAKH ERP'})
    output = io.BytesIO(); writer.write(output)
    data = output.getvalue(); _validate_pdf(data)
    return data


def _render_chromium_documents(documents, binary):
    if not binary:
        raise ValueError('missing-chromium')
    import websocket
    parts = []
    deadline = time.monotonic() + TIMEOUT_SECONDS
    with tempfile.TemporaryDirectory(prefix='jakh-report-pdf-') as temporary:
        directory = Path(temporary)
        args = [binary, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                    '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
                    '--disable-sync', '--disable-domain-reliability', '--disable-client-side-phishing-detection',
                    '--metrics-recording-only', '--password-store=basic',
                    '--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication',
                    '--host-resolver-rules=MAP * ~NOTFOUND', '--proxy-server=http://127.0.0.1:9',
                    '--proxy-bypass-list=<-loopback>', '--remote-debugging-address=127.0.0.1',
                    '--remote-debugging-port=0', '--user-data-dir=%s' % (directory / 'profile'), 'about:blank']
        # No --no-sandbox, credentials, browser session or user profile is reused.
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        connection = None
        try:
            port_file = directory / 'profile' / 'DevToolsActivePort'
            while not port_file.is_file():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError('chromium-start-failed')
                time.sleep(0.05)
            port_lines = port_file.read_text().splitlines()
            if len(port_lines) != 2 or not port_lines[0].isdigit() or not port_lines[1].startswith('/devtools/browser/'):
                raise ValueError('invalid-local-browser-endpoint')
            # A custom loopback socket prevents proxy/environment routing.
            browser_socket = socket.create_connection(('127.0.0.1', int(port_lines[0])), timeout=5)
            connection = websocket.create_connection('ws://127.0.0.1:%s%s' % tuple(port_lines),
                socket=browser_socket, suppress_origin=True, timeout=5, enable_multithread=False)
            sequence = 0
            def call(method, parameters=None, session=None):
                nonlocal sequence
                sequence += 1
                payload = {'id': sequence, 'method': method, 'params': parameters or {}}
                if session:
                    payload['sessionId'] = session
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('chromium-timeout')
                connection.settimeout(remaining)
                connection.send(json.dumps(payload))
                while True:
                    response = connection.recv()
                    if not response or len(response) > MAX_PDF * 2:
                        raise ValueError('invalid-browser-response')
                    message = json.loads(response)
                    if message.get('id') != sequence:
                        continue
                    if 'error' in message:
                        raise ValueError('browser-command-failed')
                    return message.get('result', {})
            for source in documents:
                root = lxml_html.document_fromstring(source, parser=lxml_html.HTMLParser(encoding='utf-8', no_network=True))
                paper = json.loads(root.get('data-jakh-paper'))
                direction = root.get('dir', 'ltr')
                def template(kind):
                    nodes = root.xpath('//*[%s]' % (CLASS % kind))
                    inner = ''
                    for node in nodes:
                        node.getparent().remove(node)
                        # Native page-count slots use the browser's trusted counters.
                        for counter in node.xpath('.//span'):
                            classes = counter.get('class', '').split()
                            if 'page' in classes:
                                counter.set('class', 'pageNumber')
                            elif 'topage' in classes:
                                counter.set('class', 'totalPages')
                        inner += etree.tostring(node, encoding='unicode', method='html')
                    counter = '<div dir="ltr" style="text-align:right;font-size:8px;"><span class="pageNumber"></span> / <span class="totalPages"></span></div>' if kind == 'footer' and 'pageNumber' not in inner else ''
                    css = '<style>*{box-sizing:border-box}table{width:100%;border-collapse:collapse}td{vertical-align:top}img{max-height:17mm;max-width:40mm;object-fit:contain}.header,.footer{position:static!important;transform:none!important;margin:0!important}p,ul{margin:0;padding:0}.list-unstyled{list-style:none}.text-center{text-align:center}.w-100{width:100%}.row,.d-flex{display:flex;gap:8px}.col-6{width:50%}.footer img{max-height:6mm}.footer{font-size:8px}</style>'
                    padding = '4mm 8mm 0' if kind == 'header' else '0 8mm 3mm'
                    return css + '<div dir="%s" style="width:100%%;padding:%s;font:9px Arial,sans-serif;text-align:start;">%s%s</div>' % (direction, padding, inner, counter)
                header, footer = template('header'), template('footer')
                target = call('Target.createTarget', {'url': 'about:blank'})['targetId']
                session = call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                call('Page.enable', session=session)
                call('Network.enable', session=session)
                call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*', 'file://*', 'ftp://*', 'ws://*', 'wss://*']}, session)
                frame = call('Page.getFrameTree', session=session)['frameTree']['frame']['id']
                call('Page.setDocumentContent', {'frameId': frame, 'html': etree.tostring(root, encoding='unicode', method='html')}, session)
                call('Runtime.evaluate', {'expression': 'Promise.all([document.fonts.ready,...Array.from(document.images).map(image=>image.complete?Promise.resolve():new Promise(resolve=>{image.onload=resolve;image.onerror=resolve}))])', 'awaitPromise': True, 'returnByValue': True}, session)
                result = call('Page.printToPDF', {'printBackground': True, 'preferCSSPageSize': True,
                    'paperWidth': paper['width'] / 25.4, 'paperHeight': paper['height'] / 25.4,
                    'marginTop': paper['top'] / 25.4, 'marginBottom': paper['bottom'] / 25.4,
                    'marginLeft': paper['left'] / 25.4, 'marginRight': paper['right'] / 25.4,
                    'displayHeaderFooter': True, 'headerTemplate': header, 'footerTemplate': footer}, session)
                pdf = base64.b64decode(result['data'], validate=True)
                _validate_pdf(pdf); parts.append(pdf)
                call('Target.closeTarget', {'targetId': target})
        except websocket.WebSocketException as exc:
            raise ValueError('local-browser-connection-failed') from exc
        finally:
            if connection:
                connection.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
    return _jakh_pdf_bytes(parts)


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _get_pdf_engine(self, report=None, default_engine='wkhtmltopdf'):
        engine = super()._get_pdf_engine(report, default_engine)
        if report and report.report_name in JAKH_REPORTS and engine == 'wkhtmltopdf' and super().get_pdf_engine_state(engine) == 'install' and _chromium_binary():
            return ENGINE
        return engine

    def report_action(self, docids, data=None, config=True):
        self.ensure_one()
        if self.report_name in JAKH_REPORTS and not data and docids and self._get_pdf_engine(self) == ENGINE:
            # Native frontend caches a single PDF-engine status globally. Keep
            # invoice/default-engine behaviour unchanged and download this
            # governed PDF through its existing permission-checked native route.
            ids = docids.ids if isinstance(docids, models.Model) else ([docids] if isinstance(docids, int) else docids)
            if not isinstance(ids, list) or any(isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0 for identity in ids):
                raise AccessError(_('Select valid JAKH report records before printing.'))
            self.env[self.model].browse(ids).check_access('read')
            context = {'lang': self.env.context.get('lang') or self.env.user.lang or 'en_US',
                       'tz': self.env.context.get('tz') or self.env.user.tz or 'UTC',
                       'allowed_company_ids': self.env.companies.ids}
            return {'type': 'ir.actions.act_url', 'target': 'download',
                    'url': '/report/pdf/%s/%s?%s' % (self.report_name, ','.join(str(identity) for identity in ids),
                                                    urlencode({'context': json.dumps(context)}))}
        return super().report_action(docids, data=data, config=config)

    @api.model
    def get_pdf_engine_state(self, engine_name):
        if engine_name == ENGINE:
            return 'ok' if _chromium_binary() else 'install'
        return super().get_pdf_engine_state(engine_name)

    @api.model
    def _run_pdf_engine(self, engine_name, html, report_ref=False, landscape=False, **kwargs):
        if engine_name != ENGINE:
            return super()._run_pdf_engine(engine_name, html, report_ref, landscape=landscape, **kwargs)
        report = self._get_report(report_ref) if report_ref else None
        if not report or report.report_name not in JAKH_REPORTS:
            raise AccessError(_('The offline PDF fallback is restricted to governed JAKH reports.'))
        # Odoo 20 stores only explicit assignments in group_ids; a manager's
        # financial permission is inherited. Use the native effective group
        # closure, without escalating source-record reads.
        if not self.env.su and report.group_ids and not (report.group_ids & self.env.user.all_group_ids):
            raise AccessError(_('Your current role cannot print this JAKH report.'))
        if not self.env[report.model].has_access('read'):
            raise AccessError(_('Your current role cannot read this JAKH report source.'))
        try:
            paper = _paper_spec(report.get_paperformat(), landscape)
            documents, ids = _offline_documents(html, paper, self.env.company.primary_color)
            source_ids = [identity for identity in ids if identity]
            if source_ids:
                self.env[report.model].browse(source_ids).check_access('read')
            return _render_chromium_documents(documents, _chromium_binary()), ids
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            raise UserError(_('The offline JAKH PDF could not be rendered safely. Check the configured browser, paper margins and report size.')) from exc

    def _render_qweb_pdf(self, report_ref, res_ids=None, data=None):
        content, output_format = super()._render_qweb_pdf(report_ref, res_ids, data)
        report = self._get_report(report_ref)
        if output_format == 'pdf' and report.report_name in JAKH_REPORTS:
            # Native PDF merge writers inject their vendor name in metadata.
            # Rebrand these three JAKH exports only, never native invoice semantics.
            content = _jakh_pdf_bytes([content])
        return content, output_format
