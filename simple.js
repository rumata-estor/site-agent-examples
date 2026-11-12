/* Three small guided demonstrations: real public Python modules in Pyodide. */
(() => {
  'use strict';
  const id = name => document.getElementById(name);
  const definitions = {
    task: { tab:'task', worker:'worker.js', label:'задачи' },
    change: { tab:'changeset', worker:'changeset-worker.js', label:'изменения' },
    state: { tab:'state', worker:'state-worker.js', label:'конфликты' },
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
    for (let n = 1; n <= 3; n++) setStep(config,n,'Ожидает проверки','');
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
    if (!answer.result) throw Error('Python не вернул результат операции');
    return answer.result;
  }
  function assertResult(condition, message) {
    if (!condition) throw Error(message);
  }
  function setTaskScene(task, note) {
    const state=id('task-state');
    state.className='scene-value scene-status';
    if(task?.status==='running') {state.textContent='Выполняется';state.classList.add('live');}
    else if(task?.status==='done') {state.textContent='Завершена';state.classList.add('live');}
    else state.textContent='Не запущена';
    id('task-note').textContent=note;
  }
  function setChangeScene(change, note) {
    const state=id('change-state');
    state.className='scene-value scene-status';
    if(change?.state==='sealed') {state.textContent='План проверен и зафиксирован';state.classList.add('live');}
    else if(change?.state==='draft' && change?.approval_required) {state.textContent='Высокий риск — нужно доверенное разрешение';state.classList.add('stopped');}
    else if(change?.state==='draft') state.textContent='План подготовлен';
    else state.textContent='Нет плана';
    id('change-note').textContent=note;
  }
  function setStateScene(state, note) {
    id('state-text').textContent='«'+(state?.object?.value?.content ?? 'Исходный текст страницы')+'»';
    const observed=id('state-observed');
    if(configIsRead(state)) {
      const old=state.agents.B.matches_current;
      observed.textContent=old?'Та же версия':'Исходный текст — уже устарел';
    } else observed.textContent='Ещё не читал';
    id('state-note').textContent=note;
  }
  function configIsRead(state) {return Boolean(state?.agents?.B?.has_read);}
  async function taskStep(config,n) {
    if(n===1){
      const data=await run(config,{action:'create',objective:'Обновить страницу сайта'});
      assertResult(data.status==='ok' && data.current?.status==='running','Первая задача не перешла в состояние running');
      config.details.taskId=data.selected_id;
      setTaskScene(data.current,'Задача запущена. Теперь попробуйте открыть вторую.');
      return {summary:'Первая задача запущена',explanation:'Система сохранила задачу и отметила её как выполняющуюся.',kind:'ok'};
    }
    if(n===2){
      const data=await run(config,{action:'create',objective:'Вторая параллельная задача'});
      assertResult(data.status==='rejected' && /Active task/.test(data.message) && data.current?.status==='running','Вторая активная задача не была ожидаемо отклонена');
      setTaskScene(data.current,'Вторая задача не создана. Первая продолжает выполняться.');
      return {summary:'Вторая задача заблокирована — защита сработала',explanation:'Система не допустила одновременного запуска двух занятых задач.',kind:'blocked'};
    }
    const data=await run(config,{action:'transition',task_id:config.details.taskId,new_status:'done',reason:'Демонстрационная задача завершена'});
    assertResult(data.status==='ok' && data.current?.status==='done' && !data.active_id,'Первая задача не завершена корректно');
    setTaskScene(data.current,'Первая задача завершена. Конфликт не возник.');
    return {summary:'Готово: все три действия проверены',explanation:'Задача запущена, конфликт предотвращён, завершение прошло успешно.',kind:'ok'};
  }
  async function changeStep(config,n) {
    if(n===1){
      const set=await run(config,{action:'prepare',text:'Новый текст страницы',risk:'low',shared:false});
      assertResult(set.status==='ok' && set.change?.state==='draft','Обычный план не прошёл проверку');
      const sealed=await run(config,{action:'seal'});
      assertResult(sealed.status==='ok' && sealed.change?.state==='sealed' && sealed.change?.ready_for_execution,'Обычный план не зафиксирован');
      setChangeScene(sealed.change,'План подготовлен и зафиксирован. Никакой записи на сайт не было.');
      return {summary:'Обычный план принят',explanation:'Правила соблюдены: план допустим и не требует дополнительного подтверждения.',kind:'ok'};
    }
    if(n===2){
      const reset=await run(config,{action:'reset'});
      assertResult(reset.status==='ok','Не удалось очистить демонстрационное состояние');
      const set=await run(config,{action:'prepare',text:'Рискованное изменение страницы',risk:'high',shared:false});
      assertResult(set.status==='ok' && set.change?.approval_required && set.change?.state==='draft','Опасный план не потребовал подтверждения');
      const sealed=await run(config,{action:'seal'});
      assertResult(sealed.status==='rejected' && /approval/i.test(sealed.message) && !sealed.change.ready_for_execution,'Опасный план не был отклонён');
      setChangeScene(sealed.change,'Высокий риск: без доверенного подтверждения план нельзя зафиксировать.');
      return {summary:'Опасный план остановлен — защита сработала',explanation:'Система требует дополнительного разрешения перед фиксацией плана высокого риска.',kind:'blocked'};
    }
    const approved=await run(config,{action:'approve'});
    assertResult(approved.status==='rejected' && /trusted approver/.test(approved.message) && !approved.change.ready_for_execution,'Подтверждение без полномочий не было отклонено');
    setChangeScene(approved.change,'Кнопка не подтверждает личность человека, поэтому разрешение не выдано.');
    return {summary:'Подтверждение без полномочий отклонено',explanation:'Даже явный запрос на подтверждение не проходит без доверенного механизма авторизации.',kind:'blocked'};
  }
  async function stateStep(config,n) {
    if(n===1){
      const a=await run(config,{action:'read_a'});
      const b=await run(config,{action:'read_b'});
      assertResult(a.status==='ok' && b.status==='ok' && b.state.agents.A.fingerprint===b.state.agents.B.fingerprint && b.state.agents.B.matches_current,'Агенты не прочитали одинаковую версию');
      config.details.initialContent=b.state.object.value.content;
      setStateScene(b.state,'Оба агента прочитали исходную версию текста.');
      return {summary:'Оба агента прочитали один текст',explanation:'Их исходные версии совпадают. Теперь изменим текст от имени агента А.',kind:'ok'};
    }
    if(n===2){
      const value='Новая версия от агента А';
      const a=await run(config,{action:'write_a',text_a:value});
      assertResult(a.status==='ok' && a.state.object.value.content===value && a.state.agents.B.matches_current===false,'Изменение А не удалось или версия Б не устарела');
      setStateScene(a.state,'А сохранил новый текст, а Б всё ещё опирается на старую версию.');
      return {summary:'Агент А сохранил новую версию',explanation:'Изменение принято, но данные агента Б теперь устарели.',kind:'ok'};
    }
    const b=await run(config,{action:'write_b',text_b:'Устаревшая попытка агента Б'});
    assertResult(b.status==='rejected' && /STALE_STATE/.test(b.message) && b.state.marker?.revision>=1 && b.state.object.value.content==='Новая версия от агента А','Устаревшая запись Б не была безопасно отклонена');
    setStateScene(b.state,'Устаревшая запись Б остановлена. Новая версия агента А сохранена.');
    return {summary:'Старая версия не затёрла новую — проверка пройдена',explanation:'Тестовое хранилище отказало в записи, а State Guard записал маркер конфликта.',kind:'blocked'};
  }
  async function reset(config) {
    if(!config.ready || config.busy) return;
    config.busy=true;updateControls(config);
    try{
      const data=await run(config,{action:'reset'});
      assertResult(data.status==='ok','Состояние не удалось сбросить');
      config.step=0;config.details={};clearStepMessages(config);
      if(config.key==='task') setTaskScene(null,'Начните с первой кнопки.');
      if(config.key==='change') setChangeScene(null,'Никаких изменений на настоящих сайтах не производится.');
      if(config.key==='state') setStateScene(data.state,'Оба агента снова могут прочитать исходный текст.');
      setResult(config,'','Проверка ещё не началась','Нажмите первую кнопку.');
    }catch(error){setResult(config,'fail','Не удалось начать заново',error.message);}
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
      setStep(config,n,'Не удалось проверить','fail');
      setResult(config,'fail','Проверка не пройдена',error.message);
    }finally{config.busy=false;updateControls(config);}
  }
  function setupWorker(config) {
    if(config.loading) return;
    config.loading=true;
    config.loadingElement.textContent='Загружаем Python-код в браузере…';
    config.workerInstance=new Worker('./'+config.worker);
    config.workerInstance.onmessage=({data})=>{
      const pending=config.pending.get(data.id);
      if(!pending) return;
      config.pending.delete(data.id);
      if(data.kind==='error') pending.reject(new Error(data.error));
      else pending.resolve(data);
    };
    config.workerInstance.onerror=event=>{
      config.loadingElement.className='status error';
      config.loadingElement.textContent='Ошибка загрузки Python. Обновите страницу.';
      for(const pending of config.pending.values()) pending.reject(new Error(event.message));
      config.pending.clear();
    };
    call(config,'init').then(()=>{
      config.ready=true;
      config.loadingElement.className='status ready';
      config.loadingElement.textContent='✓ Готово. Нажимайте кнопки по порядку.';
      updateControls(config);
    }).catch(error=>{
      config.loadingElement.className='status error';
      config.loadingElement.textContent='Не удалось загрузить Python: '+error.message;
    });
  }
  window.addEventListener('siteagent-tab-selected',({detail})=>{
    const key=detail.name==='changeset'?'change':detail.name==='state'?'state':'task';
    setupWorker(definitions[key]);
  });
  const current=id('tab-changeset').getAttribute('aria-selected')==='true'?'change':id('tab-state').getAttribute('aria-selected')==='true'?'state':'task';
  setupWorker(definitions[current]);
})();
