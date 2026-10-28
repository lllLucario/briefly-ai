// Independent decoded piece evidence before node-word-extractor applies character/paragraph cleanup.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const [source,pkg,out]=process.argv.slice(2);
 const Extractor=require(path.resolve(pkg,'lib/word-ole-extractor.js'));
 const Reader=require(path.resolve(pkg,'lib/buffer-reader.js'));
 const extractor=new Extractor(),reader=new Reader(fs.readFileSync(source));
 let pieces,prms,grpprls;
 const original=extractor.writePieces;
 extractor.writePieces=function(word,table){
  original.call(this,word,table);
  pieces=this._pieces.map(p=>({...p}));
  let pos=word.readUInt32LE(0x01a2);grpprls=[];
  while(table[pos]===1){const length=table.readUInt16LE(pos+1);grpprls.push(table.subarray(pos+3,pos+3+length).toString('hex'));pos+=3+length;}
  assert.equal(table[pos++],2);const bytes=table.readUInt32LE(pos);pos+=4;
  const count=(bytes-4)/12;assert.equal(count,pieces.length);prms=[];
  for(let i=0;i<count;i++){const pcd=pos+(count+1)*4+i*8;prms.push(table.readUInt16LE(pcd+6));}
 };
 await reader.open();try{await extractor.extract(reader);}finally{await reader.close();}
 const whole=pieces.map(p=>p.text).join('');
 const main=whole.slice(0,extractor._boundaries.ccpText);
 fs.mkdirSync(out,{recursive:true});
 fs.writeFileSync(path.join(out,'decoded-pieces.json'),JSON.stringify({note:'Decoded PCD text before property cleanup, not untouched binary bytes. Unicode decoding uses this independent library.',boundaries:extractor._boundaries,pieces:pieces.map((p,i)=>({...p,prm:prms[i]})),complex_grpprls_hex:grpprls},null,2));
 fs.writeFileSync(path.join(out,'main-pre-properties.json'),JSON.stringify({text:main}));
 const markers=[];for(let cp=23000;cp<27000;cp++){if([7,12,13,19,20,21].includes(main.charCodeAt(cp)))markers.push({cp,code:main.charCodeAt(cp)});}
 fs.writeFileSync(path.join(out,'region-markers.json'),JSON.stringify(markers,null,2));
 console.log(JSON.stringify({mainUtf16Length:main.length,pieces:pieces.length,regionMarkers:markers.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});
