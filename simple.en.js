/* Three small guided demonstrations: real public Python modules in Pyodide. */
(() => {
  'use strict';
  const id = name => document.getElementById(name);
  const errorText = message => /[\u0400-\u04FF]/.test(message) ? 'The operation failed. Please restart the demo.' : message;
  const definitions = {
    task: { tab:'task', worker:'worker.js', label:'tasks' },
    change: { tab:'changeset', worker:'changeset-worker.js', label:'changes' },
    state: { tab:'state', worker:'state-worker.js', label:'conflicts' },
  };
  for (const [key, config] of Object.entries(definitions)) {
    config.key = key;
    config.ready = false;
    config.busy = false;
    config.loading = false;
    config.workerInstance = null;
    config.counter = 0;
    config.pending = new Map();
    config.step = 0;
    config.details = {};
    config.controls = [1,2,3].map(n => id(`${key}-${n}`));
    config.loadingElement = id(`${key}-loading`);
    config.result = id(`${key}-result`);
    for (let n = 1; n <= 3; n++) config.controls[n-1].addEventListener('click', () => performStep(config,n));
    id(`${key}-reset`).addEventListener('click', () => reset(config));
  }

  function setResult(config, kind, title, explanation) {
    config.result.className = 'result ' + kind;
    config.result.replaceChildren();
    const strong = document.createElement('strong');
    strong.textContent = title;
    const p = document.createElement('p');
    p.textContent = explanation;
    config.result.append(strong,p);
  }
  function updateControls(config) {
    config.controls.forEach((button,index) => {
      button.disabled = !config.ready || config.busy || index !== config.step;
      button.classList.toggle('current',!button.disabled);
    });
    id(`${config.key}-reset`).disabled = !config.ready || config.busy;
  }
  function setStep(config, n, text, kind) {
    const node = id(`${config.key}-step-${n}`);
    node.className = 'step-result ' + kind;
    node.textContent = text;
  }
  function clearStepMessages(config) {
    for (let n = 1; n <= 3; n++) setStep(config,n,'Not tested yet','');
  }
  function call(config, kind, payload = {}) {
    const requestId = ++config.counter;
    return new Promise((resolve,reject) => {
      config.pending.set(requestId,{resolve,reject});
      config.workerInstance.postMessage({id:requestId,kind,payload});
    });
  }
  async function run(config,payload) {
    const answer = await call(config,'action',payload);
    if (!answer.result) throw Error('Python did not return an operation result');
    return answer.result;
  }
  function assertResult(condition, message) {
    if (!condition) throw Error(message);
  }
  function setTaskScene(task, note) {
    const state=id('task-state');
    state.className='scene-value scene-status';
    if(task?.status==='running') {state.textContent='Running';state.classList.add('live');}
    else if(task?.status==='done') {state.textContent='Completed';state.classList.add('live');}
    else state.textContent='Not started';
    id('task-note').textContent=note;
  }
  function setChangeScene(change, note) {
    const state=id('change-state');
    state.className='scene-value scene-status';
    if(change?.state==='sealed') {state.textContent='Plan validated and finalized';state.classList.add('live');}
    else if(change?.state==='draft' && change?.approval_required) {state.textContent='High risk — trusted approval required';state.classList.add('stopped');}
    else if(change?.state==='draft') state.textContent='Plan prepared';
    else state.textContent='No plan';
    id('change-note').textContent=note;
  }
  function setStateScene(state, note) {
    id('state-text').textContent='“'+(state?.object?.value?.content ?? 'Original page text')+'”';
    const observed=id('state-observed');
    if(configIsRead(state)) {
      const old=state.agents.B.matches_current;
      observed.textContent=old?'Same version':'Original version — now outdated';
    } else observed.textContent='Not read yet';
    id('state-note').textContent=note;
  }
  function configIsRead(state) {return Boolean(state?.agents?.B?.has_read);}
  async function taskStep(config,n) {
    if(n===1){
      const data=await run(config,{action:'create',objective:'Update a website page'});
      assertResult(data.status==='ok' && data.current?.status==='running','The first task did not enter the running state');
      config.details.taskId=data.selected_id;
      setTaskScene(data.current,'The task is running. Now try to start a second one.');
      return {summary:'First task started',explanation:'The system saved the task and marked it as running.',kind:'ok'};
    }
    if(n===2){
      const data=await run(config,{action:'create',objective:'Second concurrent task'});
      assertResult(data.status==='rejected' && /Active task/.test(data.message) && data.current?.status==='running','The second active task was not rejected as expected');
      setTaskScene(data.current,'The second task was not created. The first is still running.');
      return {summary:'Second task blocked — protection worked',explanation:'The system prevented two tasks from running at the same time.',kind:'blocked'};
    }
    const data=await run(config,{action:'transition',task_id:config.details.taskId,new_status:'done',reason:'Demo task completed'});
    assertResult(data.status==='ok' && data.current?.status==='done' && !data.active_id,'The first task did not complete correctly');
    setTaskScene(data.current,'The first task is complete. No conflict occurred.');
    return {summary:'Done: all three steps completed',explanation:'The task started, a conflict was prevented, and completion succeeded.',kind:'ok'};
  }
  async function changeStep(config,n) {
    if(n===1){
      const set=await run(config,{action:'prepare',text:'New page text',risk:'low',shared:false});
      assertResult(set.status==='ok' && set.change?.state==='draft','The regular plan failed validation');
      const sealed=await run(config,{action:'seal'});
      assertResult(sealed.status==='ok' && sealed.change?.state==='sealed' && sealed.change?.ready_for_execution,'The regular plan was not finalized');
      setChangeScene(sealed.change,'The plan was prepared and finalized. Nothing was written to a website.');
      return {summary:'Regular plan accepted',explanation:'The plan meets the rules and requires no additional approval.',kind:'ok'};
    }
    if(n===2){
      const reset=await run(config,{action:'reset'});
      assertResult(reset.status==='ok','Could not clear the demo state');
      const set=await run(config,{action:'prepare',text:'Risky page change',risk:'high',shared:false});
      assertResult(set.status==='ok' && set.change?.approval_required && set.change?.state==='draft','The risky plan did not require approval');
      const sealed=await run(config,{action:'seal'});
      assertResult(sealed.status==='rejected' && /approval/i.test(sealed.message) && !sealed.change.ready_for_execution,'The risky plan was not rejected');
      setChangeScene(sealed.change,'High risk: the plan cannot be finalized without trusted approval.');
      return {summary:'Risky plan blocked — protection worked',explanation:'The system requires additional authorization before finalizing a high-risk plan.',kind:'blocked'};
    }
    const approved=await run(config,{action:'approve'});
    assertResult(approved.status==='rejected' && /trusted approver/.test(approved.message) && !approved.change.ready_for_execution,'Unauthorized approval was not rejected');
    setChangeScene(approved.change,'A browser button does not verify a person’s identity, so approval was denied.');
    return {summary:'Unauthorized approval rejected',explanation:'An explicit approval request still fails without a trusted authorization mechanism.',kind:'blocked'};
  }
  async function stateStep(config,n) {
    if(n===1){
      const a=await run(config,{action:'read_a'});
      const b=await run(config,{action:'read_b'});
      assertResult(a.status==='ok' && b.status==='ok' && b.state.agents.A.fingerprint===b.state.agents.B.fingerprint && b.state.agents.B.matches_current,'The agents did not read the same version');
      config.details.initialContent=b.state.object.value.content;
      setStateScene(b.state,'Both agents read the original text.');
      return {summary:'Both agents read the same text',explanation:'Both agents have the same version. Now let agent A change the text.',kind:'ok'};
    }
    if(n===2){
      const value='New version from agent A';
      const a=await run(config,{action:'write_a',text_a:value});
      assertResult(a.status==='ok' && a.state.object.value.content===value && a.state.agents.B.matches_current===false,'Agent A’s change failed or agent B’s version is not stale');
      setStateScene(a.state,'A saved the new text, but B still has an outdated version.');
      return {summary:'Agent A saved the new version',explanation:'The update succeeded, but agent B’s data is now stale.',kind:'ok'};
    }
    const b=await run(config,{action:'write_b',text_b:'Outdated write attempted by agent B'});
    assertResult(b.status==='rejected' && /STALE_STATE/.test(b.message) && b.state.marker?.revision>=1 && b.state.object.value.content==='New version from agent A','Agent B’s stale write was not safely rejected');
    setStateScene(b.state,'Agent B’s stale write was blocked. Agent A’s new version was preserved.');
    return {summary:'The old version did not overwrite the new one — test passed',explanation:'The test store rejected the write, and State Guard recorded a conflict marker.',kind:'blocked'};
  }
  async function reset(config) {
    if(!config.ready || config.busy) return;
    config.busy=true;updateControls(config);
    try{
      const data=await run(config,config.key==='state' ? {action:'reset',initial_text:'Original page text'} : {action:'reset'});
      assertResult(data.status==='ok','Could not reset the state');
      config.step=0;config.details={};clearStepMessages(config);
      if(config.key==='task') setTaskScene(null,'Start with the first button.');
      if(config.key==='change') setChangeScene(null,'No real websites are modified.');
      if(config.key==='state') setStateScene(data.state,'Both agents can now read the original text again.');
      setResult(config,'','Demo not started','Press the first button.');
    }catch(error){setResult(config,'fail','Could not restart',error.message);}
    finally{config.busy=false;updateControls(config);}
  }
  async function performStep(config,n) {
    if(!config.ready || config.busy || n!==config.step+1) return;
    config.busy=true;updateControls(config);
    try {
      const result=await (config.key==='task' ? taskStep(config,n) : config.key==='change' ? changeStep(config,n) : stateStep(config,n));
      config.step=n;
      setStep(config,n,result.summary,result.kind);
      setResult(config,result.kind==='blocked'?'blocked':'pass',result.summary,result.explanation);
    } catch(error){
      setStep(config,n,'Could not complete the check','fail');
      setResult(config,'fail','Check failed',error.message);
    }finally{config.busy=false;updateControls(config);}
  }
  function setupWorker(config) {
    if(config.loading) return;
    config.loading=true;
    config.loadingElement.textContent='Loading Python code in the browser…';
    config.workerInstance=new Worker('./'+config.worker);
    config.workerInstance.onmessage=({data})=>{
      const pending=config.pending.get(data.id);
      if(!pending) return;
      config.pending.delete(data.id);
      if(data.kind==='error') pending.reject(new Error(errorText(data.error)));
      else pending.resolve(data);
    };
    config.workerInstance.onerror=event=>{
      config.loadingElement.className='status error';
      config.loadingElement.textContent='Python loading error. Reload the page.';
      for(const pending of config.pending.values()) pending.reject(new Error(event.message));
      config.pending.clear();
    };
    call(config,'init').then(async()=>{
      if (config.key === 'state') {
        const initial = await run(config,{action:'reset',initial_text:'Original page text'});
        assertResult(initial.status === 'ok', 'Could not initialize State Guard demo');
      }
      config.ready=true;
      config.loadingElement.className='status ready';
      config.loadingElement.textContent='✓ Ready. Press the buttons in order.';
      updateControls(config);
    }).catch(error=>{
      config.loadingElement.className='status error';
      config.loadingElement.textContent='Could not load Python: '+error.message;
    });
  }
  window.addEventListener('siteagent-tab-selected',({detail})=>{
    const key=detail.name==='changeset'?'change':detail.name==='state'?'state':'task';
    setupWorker(definitions[key]);
  });
  const current=id('tab-changeset').getAttribute('aria-selected')==='true'?'change':id('tab-state').getAttribute('aria-selected')==='true'?'state':'task';
  setupWorker(definitions[current]);
})();
