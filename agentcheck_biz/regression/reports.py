"""JSON, Markdown and JUnit share one four-state report and fixed denominator."""
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True)


def md(value):
    text = str(value).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    for char in ('\\','`','*','_','[',']','|'):
        text = text.replace(char,'\\'+char)
    return text.replace('\r',' ').replace('\n','<br>')


def markdown(report):
    lines = ['# AgentCheck D32 — '+report['purpose'], '',
        '**'+report['status']+'** · 固定槽位 '+str(report['planned']), '',
        'harness_selftest_only 只判定测试器是否检出了预期结果；只有 candidate_business_release 是发布门禁。', '',
        '| 槽位 / 案例 | 门禁 | 基线业务 | 候选业务 | 故障覆盖 | 原因 |', '|---|---|---|---|---|---|']
    for row in report['slots']:
        def observed(side,key):return ((row.get(side) or {}).get('observed') or {}).get(key,'MISSING')
        lines.append('| '+' | '.join(md(v) for v in [row['slot_id']+' / '+row['case_id'],row['status'],
            observed('baseline','business_status'),observed('candidate','business_status'),
            observed('candidate','coverage'),row['reason']])+' |')
    for control in report['controls']:lines.extend(['',md(control['status']+': '+control['reason'])])
    for row in report['slots']:
        lines.extend(['','## '+md(row['slot_id']+' '+row['profile']),''])
        alignment = row.get('alignment')
        if alignment:
            lines.extend([md('Attribution: '+alignment['attribution']+' — '+alignment['attribution_reason']),''])
            for diff in alignment['differences']+alignment['changes']:
                lines.extend([md(diff['field']+': '+encoded(diff['baseline'])+' → '+encoded(diff['candidate'])),''])
            if alignment['missing_conditions']:lines.extend([md('Missing conditions: '+', '.join(alignment['missing_conditions'])),''])
        for side in ('baseline','candidate'):
            snap = row.get(side)
            if not snap:continue
            lines.extend([md(side+': '+encoded(snap.get('observed'))),''])
            gate = snap.get('contract') or {}
            if gate.get('exception'):lines.extend([md('Explicit exception: '+encoded(gate['exception'])),''])
            for inv in (snap.get('facts') or {}).get('invariants',[]):
                lines.extend([md(side+' / '+inv['id']+': '+inv['status']+'; actual='+encoded(inv['actual'])+'; '+inv['reason']+'; evidence='+encoded(inv['evidence'])),''])
        for diff in row['invariant_deltas']:
            lines.extend([md(diff['id']+': '+diff['baseline']['status']+' → '+diff['candidate']['status']+'; new_violation='+str(diff['new_violation'])),''])
    lines.extend(['','ERROR → JUnit error；FAIL → failure；INCONCLUSIVE → skipped，命令仍返回非零。',
        'N/A 表示该案例未触发此不变量条件，不能作为该故障的覆盖证明。', '',md('Policy: '+encoded(report['policy'])),''])
    return '\n'.join(lines)


def xml_text(value):
    return re.sub(r'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]','\ufffd',str(value))


def junit(report):
    entries = [(r['slot_id'],r['status'],r['reason'],r) for r in report['slots']]
    entries += [('control-'+str(i),r['status'],r['reason'],r) for i,r in enumerate(report['controls'],1)]
    suite = ET.Element('testsuite',name=report['purpose'],tests=str(len(entries)),
        failures=str(sum(s=='FAIL' for _,s,_,_ in entries)),errors=str(sum(s=='ERROR' for _,s,_,_ in entries)),
        skipped=str(sum(s=='INCONCLUSIVE' for _,s,_,_ in entries)))
    props = ET.SubElement(suite,'properties')
    for key in ('schema_version','suite','purpose','status','planned','policy_sha256'):
        ET.SubElement(props,'property',name=key,value=str(report[key]))
    for name,status,reason,data in entries:
        node = ET.SubElement(suite,'testcase',classname=report['purpose'],name=xml_text(name))
        props = ET.SubElement(node,'properties')
        for side in ('baseline','candidate'):
            raw = ((data.get(side) or {}).get('observed') or {})
            for key in ('business_status','evidence_status','coverage','observation_status'):
                ET.SubElement(props,'property',name=side+'.'+key,value=xml_text(raw.get(key,'MISSING')))
        tag = {'FAIL':'failure','ERROR':'error','INCONCLUSIVE':'skipped'}.get(status)
        if tag:ET.SubElement(node,tag,message=xml_text(reason),type=status).text=xml_text(encoded(data))
        ET.SubElement(node,'system-out').text=xml_text(encoded(data))
    return ET.tostring(suite,encoding='unicode',xml_declaration=True)


def export(report,directory):
    directory=Path(directory)
    for name,text in [('report.json',json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n'),
                      ('report.md',markdown(report)),('junit.xml',junit(report))]:
        (directory/name).write_text(text,encoding='utf8')
