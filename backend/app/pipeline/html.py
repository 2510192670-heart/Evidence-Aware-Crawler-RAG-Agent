"""Bounded DOM extraction with declarative CSS selectors and observed links."""
import hashlib
import json
import os
import re
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from playwright.async_api import async_playwright

from .contracts import SENSITIVE
from .curl_import import checked_url
from .observe import observe
from .requirements import verify_fields


class HtmlField(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    selector: str | None = Field(default=None, max_length=200)
    attribute: Literal['text', 'href', 'title', 'src', 'content', 'datetime'] = 'text'
    source: Literal['list', 'detail'] = 'list'

    @field_validator('selector')
    @classmethod
    def safe_selector(cls, value):
        if value and (SENSITIVE.search(value) or any(c in value for c in ['{', '}', ';'])):
            raise ValueError('invalid_or_sensitive_field')
        return value


class HtmlPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    record_selector: str = Field(min_length=1, max_length=200)
    fields: dict[str, HtmlField] = Field(min_length=1, max_length=10)
    detail_link_selector: str | None = Field(default=None, max_length=200)
    next_selector: str | None = Field(default=None, max_length=200)
    unsupported_requirements: list[str] = Field(default_factory=list, max_length=10)


# These programs are authored constants. Model selectors are arguments to
# querySelector, never interpolated JavaScript or executable snippets.
DOM_EXTRACT = """({markup,plan,detail}) => {
 const doc=new DOMParser().parseFromString(markup,'text/html');
 const value=(root,f)=>{if(!f.selector)return null;const node=root.querySelector(f.selector);
   if(!node)return null;return f.attribute==='text'?node.textContent.trim():node.getAttribute(f.attribute)};
 const fields=(root)=>Object.fromEntries(Object.entries(plan.fields).filter(([k,f])=>f.source===(detail?'detail':'list')).map(([k,f])=>[k,value(root,f)]));
 if(detail)return {values:fields(doc)};
 const nodes=Array.from(doc.querySelectorAll(plan.record_selector));
 if(nodes.length>1000)throw new Error('too_many_page_records');
 const rows=nodes.map(root=>({values:fields(root),detail:plan.detail_link_selector?root.querySelector(plan.detail_link_selector)?.getAttribute('href'):null}));
 return {rows,next:plan.next_selector?doc.querySelector(plan.next_selector)?.getAttribute('href'):null};
}"""
DOM_OUTLINE = """markup => {
 const doc=new DOMParser().parseFromString(markup,'text/html');
 const seen=new Set(), nodes=[];
 for(const node of doc.querySelectorAll('body *')){
  if(['SCRIPT','STYLE','INPUT','TEXTAREA','SELECT','OPTION','SVG','PATH'].includes(node.tagName))continue;
  const selector=node.tagName.toLowerCase()+Array.from(node.classList).slice(0,3).map(c=>'.'+CSS.escape(c)).join('');
  if(seen.has(selector)){
   const existing=nodes.find(n=>n.selector===selector);
   existing.attributes=Array.from(new Set([...existing.attributes,...Array.from(node.attributes).map(a=>a.name)]));
   continue;
  }seen.add(selector);
  nodes.push({selector,id:node.id||null,attributes:Array.from(node.attributes).map(a=>a.name),parent:node.parentElement?.tagName.toLowerCase()});
  if(nodes.length>=100)break;
 }
 const candidate=doc.querySelector('article a[href],main li a[href],tbody tr a[href]');
 return {nodes,detail_sample_link:candidate?.getAttribute('href')||null};
}"""


async def parse_dom(markup, plan=None, *, detail=False):
    async with async_playwright() as p:
        browser = await p.chromium.launch(env={k: v for k, v in os.environ.items() if not SENSITIVE.search(k)})
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.abort())
            if plan is None:
                return await page.evaluate(DOM_OUTLINE, markup)
            return await page.evaluate(DOM_EXTRACT, {'markup': markup, 'plan': plan.model_dump(), 'detail': detail})
        finally:
            await browser.close()


def checked_link(base, href, origin, policy):
    url = urljoin(base, href)
    checked_url(url, policy)
    parsed = urlsplit(url)
    if f'{parsed.scheme}://{parsed.netloc}' != origin:
        raise ValueError('cross_origin_request')
    return url


async def html_summary(record, policy=None, resolver=None):
    outline = await parse_dom(record.body['markup'])
    summary = {'request_id': 'html_document', 'mode': 'html',
               'nodes': [node for node in outline['nodes'] if not SENSITIVE.search(json.dumps(node))]}
    href = outline['detail_sample_link']
    if href:
        parts = urlsplit(record.url)
        try:
            target = checked_link(record.url, href, f'{parts.scheme}://{parts.netloc}', policy)
        except ValueError:
            # This heuristic candidate is optional evidence, not a requested
            # traversal. Reject it without blocking an admissible list page.
            return summary
        detail = (await observe(target, None, policy=policy, resolver=resolver, force_html=True))[0]
        detail_outline = await parse_dom(detail.body['markup'])
        summary['detail_nodes'] = [node for node in detail_outline['nodes'] if not SENSITIVE.search(json.dumps(node))]
    return summary


def normalize_html(values, specs, base):
    result = dict(values)
    for spec in specs:
        value = result.get(spec.name)
        if value is None:
            continue
        if spec.type in {'number', 'integer'}:
            text = value.strip()
            if not re.fullmatch(r'[+-]?\d+(\.\d+)?', text):
                raise ValueError('requested_field_type_mismatch')
            result[spec.name] = int(text) if spec.type == 'integer' and '.' not in text else float(text)
        elif spec.type == 'boolean':
            if value.lower() not in {'true', 'false'}:
                raise ValueError('requested_field_type_mismatch')
            result[spec.name] = value.lower() == 'true'
    return verify_fields([result], specs)[0][0]


async def execute_html(url, plan, specs, *, max_pages=3, max_records=100, policy=None, resolver=None):
    if not 1 <= max_pages <= 10 or not 1 <= max_records <= 1000:
        raise ValueError('collection_budget_out_of_range')
    if set(plan.fields) != {spec.name for spec in specs}:
        raise ValueError('requested_fields_mismatch')
    if plan.unsupported_requirements:
        raise ValueError('unsupported_requirements')
    for name, field in plan.fields.items():
        if SENSITIVE.search(name):
            raise ValueError('invalid_or_sensitive_field')
        if field.source == 'detail' and not plan.detail_link_selector:
            raise ValueError('detail_link_required')
    parts = urlsplit(url)
    origin = f'{parts.scheme}://{parts.netloc}'
    rows, sources, seen, visited = [], [], set(), set()
    requests, duplicates = 0, 0
    completeness = 'partial'
    for page_number in range(1, max_pages + 1):
        url = checked_link(url, url, origin, policy)
        if url in visited:
            raise ValueError('pagination_cycle')
        visited.add(url)
        requests += 1
        if requests > 60:
            raise ValueError('html_request_budget_exceeded')
        document = (await observe(url, None, policy=policy, resolver=resolver, force_html=True))[0]
        extracted = await parse_dom(document.body['markup'], plan)
        if not extracted['rows']:
            raise ValueError('no_list_sample')
        for entry in extracted['rows']:
            values = entry['values']
            source = url
            if any(field.source == 'detail' for field in plan.fields.values()):
                if not entry['detail']:
                    raise ValueError('detail_link_missing')
                source = checked_link(url, entry['detail'], origin, policy)
                requests += 1
                if requests > 60:
                    raise ValueError('html_request_budget_exceeded')
                detail = (await observe(source, None, policy=policy, resolver=resolver, force_html=True))[0]
                values.update((await parse_dom(detail.body['markup'], plan, detail=True))['values'])
            for name, field in plan.fields.items():
                if field.attribute in {'href', 'src'} and values.get(name):
                    values[name] = urljoin(source if field.source == 'detail' else url, values[name])
            row = normalize_html(values, specs, source)
            identity = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            rows.append(row)
            sources.append(source)
            if len(rows) >= max_records:
                return {'items': rows, 'pages': page_number, 'expected_total': None, 'completeness': 'partial',
                        'record_limit_reached': True, 'source_urls': sources, 'duplicates_removed': duplicates}
        if not extracted['next']:
            completeness = 'stop_condition_only'
            break
        url = checked_link(url, extracted['next'], origin, policy)
    return {'items': rows, 'pages': page_number, 'expected_total': None, 'completeness': completeness,
            'record_limit_reached': False, 'source_urls': sources, 'duplicates_removed': duplicates}
