import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const URL=process.argv[2],PORT=Number(process.argv[3]||9450),WAIT=Number(process.argv[4]||40);
const EDGE=["C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe","C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"].find(existsSync);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let pass=0,fail=0;
const check=(l,ok,d="")=>{ if(ok){pass++;console.log("  PASS  "+l);} else {fail++;console.log("  FAIL  "+l+"  "+d);} };
class Cdp{constructor(ws){this.ws=ws;this.id=0;this.p=new Map();this.events=[];
 ws.addEventListener("message",e=>{let m;try{m=JSON.parse(e.data);}catch{return;}
  if(m.id!==undefined&&this.p.has(m.id)){this.p.get(m.id)(m);this.p.delete(m.id);}else if(m.method)this.events.push(m);});}
 send(m,p={},t=40000){const id=++this.id;return new Promise((res,rej)=>{const tm=setTimeout(()=>rej(new Error("timeout "+m)),t);
  this.p.set(id,x=>{clearTimeout(tm);res(x);});this.ws.send(JSON.stringify({id,method:m,params:p}));});}
 async ev(e){const r=await this.send("Runtime.evaluate",{expression:e,returnByValue:true,awaitPromise:true});
  if(r.result&&r.result.exceptionDetails)return {__error:r.result.exceptionDetails.text};
  return r.result&&r.result.result?r.result.result.value:undefined;}}
const profile=mkdtempSync(join(tmpdir(),"sysmon-realtype-"));
const child=spawn(EDGE,["--headless=new","--disable-gpu","--no-first-run","--disable-extensions","--window-size=1600,1000",`--remote-debugging-port=${PORT}`,`--user-data-dir=${profile}`,"about:blank"],{stdio:"ignore"});
async function main(){
 let targets=null;
 for(let i=0;i<40&&!targets;i++){await sleep(500);try{const r=await fetch(`http://127.0.0.1:${PORT}/json/list`);if(r.ok)targets=await r.json();}catch{}}
 const page=targets.find(t=>t.type==="page");
 const ws=new WebSocket(page.webSocketDebuggerUrl);
 await new Promise((res,rej)=>{ws.addEventListener("open",res,{once:true});ws.addEventListener("error",rej,{once:true});});
 const cdp=new Cdp(ws);
 await cdp.send("Runtime.enable");await cdp.send("Page.enable");
 await cdp.send("Emulation.setDeviceMetricsOverride",{width:1600,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp.send("Page.navigate",{url:URL});
 await sleep(WAIT*1000);
 await cdp.ev('[...document.querySelectorAll("#sysmon-panel .sysmon-tab")].find(function(b){return b.textContent.trim()==="设置";}).click(),1');
 await sleep(2500);
 console.log("=== 真实点击 + 真实逐键输入（模拟人手打字速度） ===");
 const box=await cdp.ev(`(function(){const p=document.getElementById("sysmon-panel");
   const k=p.querySelector('.sysmon-body input[type=password]'); if(!k) return null;
   k.scrollIntoView({block:"center"}); const r=k.getBoundingClientRect();
   return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()`);
 check("找到 API Key 输入框", !!box, JSON.stringify(box));
 await cdp.send("Input.dispatchMouseEvent",{type:"mousePressed",x:box.x,y:box.y,button:"left",clickCount:1});
 await cdp.send("Input.dispatchMouseEvent",{type:"mouseReleased",x:box.x,y:box.y,button:"left",clickCount:1});
 await sleep(300);
 // Type with insertText (the correct way: one event per character).
 const TYPED="sk-abc123XYZ";
 for(const ch of TYPED){ await cdp.send("Input.insertText",{text:ch}); await sleep(220); }
 await sleep(1500);
 let v=await cdp.ev(`document.querySelector('#sysmon-panel .sysmon-body input[type=password]').value`);
 check("逐键输入完全正确，无丢字无重复", v===TYPED, JSON.stringify(v));
 // Keep typing after a long pause - the exact thing that failed before.
 for(const ch of "789"){ await cdp.send("Input.insertText",{text:ch}); await sleep(700); }
 await sleep(1200);
 v=await cdp.ev(`document.querySelector('#sysmon-panel .sysmon-body input[type=password]').value`);
 check("停顿后继续输入仍然有效", v===TYPED+"789", JSON.stringify(v));
 const foc=await cdp.ev(`document.activeElement === document.querySelector('#sysmon-panel .sysmon-body input[type=password]')`);
 check("焦点全程保持在输入框", foc===true, String(foc));
 // Other fields too.
 const base=await cdp.ev(`(function(){const p=document.getElementById("sysmon-panel");
   const b=[...p.querySelectorAll('.sysmon-body input[type=text]')][0]; return b?{x:0}:null;})()`);
 const box2=await cdp.ev(`(function(){const p=document.getElementById("sysmon-panel");
   const b=[...p.querySelectorAll('.sysmon-body input[type=text]')][0]; if(!b) return null;
   const r=b.getBoundingClientRect(); return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};})()`);
 if(box2){
   await cdp.send("Input.dispatchMouseEvent",{type:"mousePressed",x:box2.x,y:box2.y,button:"left",clickCount:1});
   await cdp.send("Input.dispatchMouseEvent",{type:"mouseReleased",x:box2.x,y:box2.y,button:"left",clickCount:1});
   await sleep(300);
   for(const ch of "https://api.deepseek.com"){ await cdp.send("Input.insertText",{text:ch}); await sleep(30); }
   await sleep(1500);
   const bv=await cdp.ev(`[...document.querySelectorAll('#sysmon-panel .sysmon-body input[type=text]')][0].value`);
   check("Base URL 字段可正常输入", bv==="https://api.deepseek.com", JSON.stringify(bv));
   await sleep(2000);
   const bv2=await cdp.ev(`[...document.querySelectorAll('#sysmon-panel .sysmon-body input[type=text]')][0].value`);
   check("Base URL 内容不会被轮询抹掉", bv2==="https://api.deepseek.com", JSON.stringify(bv2));
 }
 const errs=cdp.events.filter(e=>e.method==="Runtime.exceptionThrown");
 check("无 JS 异常", errs.length===0, String(errs.length));
 const shot=await cdp.send("Page.captureScreenshot",{format:"png"});
 writeFileSync(join(process.cwd(),"real-typing.png"),Buffer.from(shot.result.data,"base64"));
 console.log(`\npassed: ${pass}   failed: ${fail}`);
 ws.close();child.kill();process.exit(fail?1:0);
}
main().catch(e=>{console.log("FATAL "+e.message);child.kill();process.exit(2);})
 .finally(async()=>{await sleep(500);try{rmSync(profile,{recursive:true,force:true});}catch{}});
