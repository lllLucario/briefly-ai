// Independent diagnostic only. Never merged into production parsing output.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

(async () => {
  const [source, packageFolder, output] = process.argv.slice(2);
  const WordOleExtractor = require(path.resolve(packageFolder, 'lib/word-ole-extractor.js'));
  const BufferReader = require(path.resolve(packageFolder, 'lib/buffer-reader.js'));
  const data = fs.readFileSync(source);
  assert.equal(data.subarray(0,8).toString('hex'), 'd0cf11e0a1b11ae1');
  const reader = new BufferReader(data);
  const extractor = new WordOleExtractor();
  await reader.open();
  let document;
  const started = performance.now();
  try { document = await extractor.extract(reader); }
  finally { await reader.close(); }
  const options = {filterUnicode:false};
  const raw = {
    body:document.getBody(options),
    textboxes_body:document.getTextboxes({...options,includeBody:true,includeHeadersAndFooters:false}),
    textboxes_header_footer:document.getTextboxes({...options,includeBody:false,includeHeadersAndFooters:true}),
    textboxes_all:document.getTextboxes(options),
    headers:document.getHeaders({...options,includeFooters:false}),
    footers:document.getFooters(options),
    footnotes:document.getFootnotes(options),
    endnotes:document.getEndnotes(options),
    annotations:document.getAnnotations(options),
  };
  assert.equal(raw.textboxes_body, document._textboxes);
  assert.equal(raw.textboxes_header_footer, document._headerTextboxes);
  const normalized = Object.fromEntries(Object.entries(raw).map(([name,text]) =>
    [name,text.normalize('NFKC').replace(/\s+/gu,'')]));
  fs.mkdirSync(output,{recursive:true});
  for (const [name,text] of Object.entries(raw)) {
    fs.writeFileSync(path.join(output,name+'.raw.txt'),text,'utf8');
    fs.writeFileSync(path.join(output,name+'.normalized.txt'),normalized[name],'utf8');
  }
  fs.writeFileSync(path.join(output,'api-raw.json'),JSON.stringify(raw,null,2),'utf8');
  fs.writeFileSync(path.join(output,'api-normalized.json'),JSON.stringify(normalized,null,2),'utf8');
  const metadata = {
    node:process.version, parserVersion:require(path.resolve(packageFolder,'package.json')).version,
    sourceSha256:crypto.createHash('sha256').update(data).digest('hex'),
    elapsedSeconds:(performance.now()-started)/1000,
    entryPoint:'Unmodified WordOleExtractor + BufferReader; same OLE branch as WordExtractor.extract; no DOCX dependency needed.',
    filterUnicode:false,internalCleanStillApplied:true,boundaries:extractor._boundaries,
    lengths:Object.fromEntries(Object.entries(raw).map(([name,text])=>[name,text.length])),
    note:'raw means returned API string before our normalization, not untouched binary text. Textboxes are aggregated strings, not graph nodes or reading-order evidence.'
  };
  fs.writeFileSync(path.join(output,'metadata.json'),JSON.stringify(metadata,null,2),'utf8');
  console.log(JSON.stringify({sourceSha256:metadata.sourceSha256,lengths:metadata.lengths}));
})().catch(error => {console.error(error);process.exitCode=1;});
