#!/usr/bin/env node
'use strict';
// MCP stdio: one JSON-RPC message per line. stdout is protocol-only.
const fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const root=fs.realpathSync(process.env.VAULT_PATH||path.join(__dirname,'../..'));
const policy=JSON.parse(fs.readFileSync(path.join(__dirname,'role-policy.json'),'utf8'));
const role=process.env.GALAXY_AGENT||'Reader';
if(!Object.hasOwn(policy,role))throw Error('Unknown GALAXY_AGENT');
const allowed=new Set(policy[role]);
function safe(rel){
 if(typeof rel!=='string'||!rel||path.isAbsolute(rel))throw Error('relative path required');
 const target=path.resolve(root,rel);
 if(target!==root&&!target.startsWith(root+path.sep))throw Error('path outside workspace');
 const parts=path.relative(root,target).split(path.sep);
 if(parts.some(x=>x==='.git'||x.startsWith('.env')||x.endsWith('.secret')))throw Error('private path denied');
 let current=root;
 for(const part of parts){current=path.join(current,part);try{if(fs.lstatSync(current).isSymbolicLink())throw Error('symlink paths denied')}catch(e){if(e.code!=='ENOENT')throw e;}}
 return target;
}
function schema(properties,required=[]){return {type:'object',properties,required,additionalProperties:false};}
const tools=[
{name:'vault.read',description:'Read a UTF-8 note',inputSchema:schema({path:{type:'string'}},['path'])},
{name:'vault.search',description:'Search Markdown notes',inputSchema:schema({query:{type:'string'}},['query'])},
{name:'vault.append',description:'Append a line to a Markdown note',inputSchema:schema({path:{type:'string'},line:{type:'string'}},['path','line'])},
{name:'filesystem.read',description:'Read a repository file',inputSchema:schema({path:{type:'string'}},['path'])},
{name:'filesystem.write',description:'Write a repository file',inputSchema:schema({path:{type:'string'},content:{type:'string'}},['path','content'])},
{name:'terminal.run',description:'Run argv without a shell; requires explicit local opt-in',inputSchema:schema({argv:{type:'array',items:{type:'string'},minItems:1},timeout_ms:{type:'number'}},['argv'])},
{name:'git.diff',description:'Inspect Git diff',inputSchema:schema({cached:{type:'boolean'}})},
{name:'git.status',description:'Inspect Git status',inputSchema:schema({})},
{name:'git.commit',description:'Disabled: use solar.py finalize for verified commits',inputSchema:schema({message:{type:'string'}},['message'])}
];
function walk(dir,q,out=[]){for(const e of fs.readdirSync(dir,{withFileTypes:true})){if(e.name.startsWith('.')||e.isSymbolicLink()||['traces','memory','node_modules'].includes(e.name))continue;const f=path.join(dir,e.name);if(e.isDirectory())walk(f,q,out);else if(e.name.endsWith('.md')&&fs.readFileSync(f,'utf8').toLowerCase().includes(q))out.push(path.relative(root,f));}return out;}
function run(argv,timeout=120000){return cp.execFileSync(argv[0],argv.slice(1),{cwd:root,encoding:'utf8',timeout,maxBuffer:4*1024*1024,stdio:['ignore','pipe','pipe']});}
function call(name,a={}){
 if(!allowed.has(name))throw Error(`Capability denied for ${role}: ${name}`);
 if(name==='vault.read'||name==='filesystem.read'){const p=safe(a.path);if(fs.statSync(p).size>4*1024*1024)throw Error('file too large');return fs.readFileSync(p,'utf8');}
 if(name==='vault.search'){if(typeof a.query!=='string')throw Error('query required');return JSON.stringify(walk(root,a.query.toLowerCase()));}
 if(name==='vault.append'){if(typeof a.line!=='string'||/[\r\n]/.test(a.line)||!a.path.endsWith('.md'))throw Error('one line and Markdown path required');fs.appendFileSync(safe(a.path),'\n'+a.line+'\n');return 'appended';}
 if(name==='filesystem.write'){if(typeof a.content!=='string')throw Error('content required');const p=safe(a.path);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,a.content,'utf8');return 'written';}
 if(name==='terminal.run'){
  if(process.env.GALAXY_ALLOW_TERMINAL!=='1')throw Error('Terminal disabled; set GALAXY_ALLOW_TERMINAL=1 in trusted local configuration');
  if(!Array.isArray(a.argv)||!a.argv.length||!a.argv.every(x=>typeof x==='string'))throw Error('argv required');
  return run(a.argv,Math.max(1,Math.min(Number(a.timeout_ms)||120000,900000)));
 }
 if(name==='git.diff')return run(['git','diff',...(a.cached?['--cached']:[])]);
 if(name==='git.status')return run(['git','status','--short']);
 if(name==='git.commit')throw Error('Use solar.py finalize with a verified run ID');
 throw Error('unknown tool');
}
function send(message){process.stdout.write(JSON.stringify(message)+'\n');}
function dispatch(raw){
 let r;
 try{r=JSON.parse(raw)}catch(e){send({jsonrpc:'2.0',id:null,error:{code:-32700,message:'Parse error'}});return;}
 if(!r||typeof r!=='object'||r.jsonrpc!=='2.0'||typeof r.method!=='string'){send({jsonrpc:'2.0',id:r?.id??null,error:{code:-32600,message:'Invalid Request'}});return;}
 if(r.id===undefined)return;
 try{
  let result;
  if(r.method==='initialize')result={protocolVersion:'2024-11-05',capabilities:{tools:{}},serverInfo:{name:'galaxy-tools',version:'1.7.0'}};
  else if(r.method==='ping')result={};
  else if(r.method==='tools/list')result={tools:tools.filter(t=>allowed.has(t.name)&&t.name!=='git.commit'&&(t.name!=='terminal.run'||process.env.GALAXY_ALLOW_TERMINAL==='1'))};
  else if(r.method==='tools/call'){
   try{result={content:[{type:'text',text:String(call(r.params?.name,r.params?.arguments))}]};}
   catch(e){result={isError:true,content:[{type:'text',text:e.message}]};}
  }else{send({jsonrpc:'2.0',id:r.id,error:{code:-32601,message:'Method not found'}});return;}
  send({jsonrpc:'2.0',id:r.id,result});
 }catch(e){send({jsonrpc:'2.0',id:r.id,error:{code:-32603,message:e.message}});}
}
let buffer='';process.stdin.setEncoding('utf8');
process.stdin.on('data',chunk=>{buffer+=chunk;if(Buffer.byteLength(buffer)>1024*1024){process.stderr.write('message buffer too large\n');process.exitCode=1;process.stdin.destroy();return;}let i;while((i=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,i).trim();buffer=buffer.slice(i+1);if(line)dispatch(line);}});
