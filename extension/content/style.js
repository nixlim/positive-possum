// Styles for the banner and drawer shadow roots, in the patrol desk's visual language.
// No web fonts are fetched: the families apply only where installed.
const STYLE = `
:host{all:initial;display:block;
  --bg:#07090d;--panel:#0d1117;--line:#1c2530;--ink:#d8e0ea;--dim:#75829a;--faint:#3a4556;
  --frontier:#ffb547;--owned:#35e0d6;--keep:#4ade80;--revert:#ff5470;--unsure:#c6a7ff;
  font:12px/1.45 "IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink)}
*{box-sizing:border-box}
.banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:6px 0 10px;padding:7px 12px;
  background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--faint)}
.banner.low{border-left-color:var(--keep)}.banner.mid{border-left-color:var(--frontier)}.banner.high{border-left-color:var(--revert)}
.facts{flex:1;min-width:0}
.notice{display:block;color:var(--dim)}.notice code{color:var(--ink)}
.chip{font:600 10px "Chakra Petch",system-ui,sans-serif;letter-spacing:.12em;text-transform:uppercase;
  padding:2px 7px;border:1px solid;border-radius:2px;white-space:nowrap}
.chip.ok,.risk.low{color:var(--keep)}.chip.warn,.risk.mid{color:var(--frontier)}
.chip.alert,.risk.high{color:var(--revert)}.chip.dim,.risk.none{color:var(--dim)}
.chip.offline{color:var(--unsure)}
button{font:700 11px "Chakra Petch",system-ui,sans-serif;letter-spacing:.16em;text-transform:uppercase;
  padding:5px 12px;border:1px solid var(--owned);background:transparent;color:var(--owned);cursor:pointer}
button:hover{background:var(--owned);color:#031}
button:disabled{opacity:.35;cursor:default}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(460px,100vw);z-index:2147483000;display:flex;
  flex-direction:column;background:var(--bg);border-left:1px solid var(--line);box-shadow:-12px 0 32px rgba(0,0,0,.45)}
.drawer[hidden]{display:none}
header{padding:12px 14px 8px;border-bottom:1px solid var(--line);background:linear-gradient(90deg,#0d1117,#0a0e14)}
h1{font:700 15px "Chakra Petch",system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase;margin:0;flex:1}
h1 b{color:var(--owned)}
.page-title{color:#fff;font-weight:600;margin:6px 0 4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
button.close{border:0;font-size:16px;padding:0 6px;color:var(--dim)}
button.close:hover{background:transparent;color:#fff}
nav{display:flex;border-bottom:1px solid var(--line)}
nav button{flex:1;border:0;border-bottom:2px solid transparent;color:var(--dim);padding:9px 0;letter-spacing:.08em;font-size:10px}
nav button:hover{background:transparent;color:var(--ink)}
nav button.on{color:var(--owned);border-bottom-color:var(--owned)}
.body{flex:1;overflow-y:auto;padding:12px 14px}
footer{padding:8px 14px;border-top:1px dashed var(--line);color:var(--dim);font-size:10.5px}
footer .changed{color:var(--revert);font-weight:700;background:rgba(255,84,112,.12);padding:0 4px}
.forced{color:var(--frontier);font-size:11px;margin-top:4px}
.refresh{margin:4px 0 12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.card{display:block;text-decoration:none;color:inherit;border:1px solid var(--line);border-left:3px solid var(--faint);
  background:var(--panel);margin-bottom:10px;padding:9px 11px}
a.card:hover{border-color:#2d3a4b}
.card.ok{border-left-color:var(--keep)}.card.warn{border-left-color:var(--frontier)}.card.alert{border-left-color:var(--revert)}
.tag{font-size:10px;letter-spacing:.08em;padding:1px 6px;border-radius:2px;background:#131a23;color:var(--dim)}
.tag.warn{color:var(--frontier)}
.time{margin-left:auto;color:var(--faint);font-size:10px}
.rationale{color:#fff;margin-top:6px}
.sub{color:var(--dim);font-size:11px;margin-top:4px}
ul.claims{margin:6px 0 0;padding:0;list-style:none;font-size:11px}
.claims .add{color:#bdf5c8}.claims .del{color:#ffc3cd}
.note{color:var(--dim);font-size:11px;margin:0 0 10px}
.empty{color:var(--faint);font-size:11px;letter-spacing:.06em}
dl.kv{display:grid;grid-template-columns:1fr auto;gap:4px 14px;margin:8px 0 14px;font-size:11.5px}
.kv dt{color:var(--dim)}.kv dd{margin:0;text-align:right;color:#fff}
h3,h4{font:700 11px "Chakra Petch",system-ui,sans-serif;letter-spacing:.2em;text-transform:uppercase;color:var(--dim);margin:14px 0 6px}
h4{letter-spacing:.12em;font-size:10px;color:var(--faint)}
table{width:100%;border-collapse:collapse;font-size:11px;margin-bottom:12px}
th{text-align:left;color:var(--dim);font-weight:500;border-bottom:1px solid var(--line);padding:4px}
td{padding:4px;border-bottom:1px solid #121821}
td.name{max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#fff}
.n{text-align:right}
tr.controversy td:first-child{color:var(--frontier)}
.grow{color:var(--keep)}.shrink{color:var(--revert)}
svg.pulse{display:block;width:100%;height:auto;background:var(--panel);border:1px solid var(--line);margin-bottom:8px}
.pulse .line{fill:none;stroke:var(--owned);stroke-width:1.4}
.pulse .burst{fill:rgba(255,181,71,.18)}
.pulse .flag{stroke:var(--bg);stroke-width:1}
.pulse .sev0,.pulse .sev1{fill:var(--unsure)}.pulse .sev2{fill:var(--frontier)}.pulse .sev3{fill:var(--revert)}
.pulse text{fill:var(--dim);font-size:9px;font-family:inherit}
ul.served,ul.shift{margin:4px 0 8px;padding:0;list-style:none}
.shift .alert{color:var(--revert)}.shift .warn{color:var(--frontier)}
.md h3{color:var(--owned)}
.md p{margin:0 0 8px}
.md ul,.md ol{margin:0 0 8px;padding-left:18px}
`;
