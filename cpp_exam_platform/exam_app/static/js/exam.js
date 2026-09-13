(() => {
  const app = document.getElementById('examApp');
  if (!app) return;
  const attemptId = app.dataset.attempt;
  const monitoring = app.dataset.monitor === '1';
  const requireFullscreen = app.dataset.fullscreen === '1';
  const blockCopy = app.dataset.blockCopy === '1';
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let seconds = Number(app.dataset.remaining || 0);
  let intentionalSubmit = false;
  let blurTimer = null;
  const panels = [...document.querySelectorAll('.question-panel')];
  const palette = [...document.querySelectorAll('.palette-btn')];
  const saveStatus = document.getElementById('saveStatus');

  const postJSON = (url, data, keepalive=false) => fetch(url, {
    method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
    body:JSON.stringify(data), credentials:'same-origin', keepalive
  });

  function logEvent(type, details='') {
    if (!monitoring) return;
    postJSON(`/student/attempts/${attemptId}/monitor`, {type, details}, true).catch(()=>{});
  }

  function currentPanel() { return document.querySelector('.question-panel.active'); }
  function showPanel(index) {
    index = Math.max(0, Math.min(panels.length - 1, index));
    panels.forEach((p,i)=>p.classList.toggle('active', i===index));
    palette.forEach((p,i)=>p.classList.toggle('active', i===index));
    window.scrollTo({top:0, behavior:'smooth'});
  }
  palette.forEach((b,i)=>b.addEventListener('click',()=>showPanel(i)));
  document.querySelectorAll('.next-btn').forEach(b=>b.addEventListener('click',()=>showPanel(panels.indexOf(currentPanel())+1)));
  document.querySelectorAll('.prev-btn').forEach(b=>b.addEventListener('click',()=>showPanel(panels.indexOf(currentPanel())-1)));
  document.querySelectorAll('.flag-btn').forEach(b=>b.addEventListener('click',()=>{const p=currentPanel();const i=panels.indexOf(p);palette[i].classList.toggle('flagged');b.textContent=palette[i].classList.contains('flagged')?'★ Marked for review':'☆ Mark for review';}));

  let saveTimers = new Map();
  function scheduleSave(panel, value) {
    const qid = panel.dataset.question;
    palette[panels.indexOf(panel)].classList.toggle('answered', String(value).trim() !== '');
    saveStatus.textContent='Saving…';
    clearTimeout(saveTimers.get(qid));
    saveTimers.set(qid,setTimeout(async()=>{
      try {const r=await postJSON(`/student/attempts/${attemptId}/save`,{question_id:qid,answer:value}); if(r.ok){saveStatus.textContent='Saved';setTimeout(()=>saveStatus.textContent='Answers save automatically',1200);} }
      catch(e){saveStatus.textContent='Save interrupted — retrying';}
    },450));
  }
  panels.forEach(panel=>{
    panel.querySelectorAll('input[type=radio]').forEach(el=>el.addEventListener('change',()=>scheduleSave(panel,el.value)));
    const short=panel.querySelector('.short-answer'); if(short) short.addEventListener('input',()=>scheduleSave(panel,short.value));
    const code=panel.querySelector('.code-editor'); if(code){
      code.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();const s=code.selectionStart,en=code.selectionEnd;code.value=code.value.substring(0,s)+'    '+code.value.substring(en);code.selectionStart=code.selectionEnd=s+4;scheduleSave(panel,code.value);}});
      code.addEventListener('input',()=>scheduleSave(panel,code.value));
    }
  });

  document.querySelectorAll('.compile-btn').forEach(btn=>btn.addEventListener('click',async()=>{
    const panel=btn.closest('.question-panel'), code=panel.querySelector('.code-editor').value;
    btn.disabled=true;btn.textContent='Compiling…';
    try{const r=await postJSON(`/student/attempts/${attemptId}/compile`,{question_id:panel.dataset.question,code});const d=await r.json();alert(d.ok?'Compilation successful.':(d.error||'Compilation failed.'));}finally{btn.disabled=false;btn.textContent='Compile';}
  }));
  document.querySelectorAll('.run-btn').forEach(btn=>btn.addEventListener('click',async()=>{
    const panel=btn.closest('.question-panel'), code=panel.querySelector('.code-editor').value, input=panel.querySelector('.stdin-box')?.value||'', out=panel.querySelector('.output-box');
    btn.disabled=true;btn.textContent='Running…';out.textContent='Running…';
    try{const r=await postJSON(`/student/attempts/${attemptId}/run`,{question_id:panel.dataset.question,code,stdin:input});const d=await r.json();out.textContent=(d.stdout||'')+(d.stderr?`\n${d.stderr}`:'')||'Program finished with no output.';}catch(e){out.textContent='Execution request failed.';}finally{btn.disabled=false;btn.textContent='Run code';}
  }));

  const timer=document.querySelector('#timer strong');
  function tick(){if(seconds<=0){timer.textContent='00:00';intentionalSubmit=true;document.querySelector('#submitModal form')?.submit();return;}const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=seconds%60;timer.textContent=(h?`${h}:`:'')+`${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;if(seconds<300)document.getElementById('timer').classList.add('urgent');seconds--;}
  tick();setInterval(tick,1000);

  const modal=document.getElementById('submitModal');document.getElementById('openSubmit')?.addEventListener('click',()=>modal.classList.add('show'));document.getElementById('cancelSubmit')?.addEventListener('click',()=>modal.classList.remove('show'));modal.querySelector('form')?.addEventListener('submit',()=>{intentionalSubmit=true;});

  if (monitoring) {
    document.addEventListener('visibilitychange',()=>{if(document.hidden)logEvent('tab_hidden','Exam page became hidden');else logEvent('tab_visible','Student returned to exam page');});
    window.addEventListener('blur',()=>{blurTimer=setTimeout(()=>{if(!document.hidden)logEvent('window_blur','Browser window lost focus');},450);});
    window.addEventListener('focus',()=>{if(blurTimer){clearTimeout(blurTimer);blurTimer=null;}logEvent('window_focus','Student returned to exam window');});
    document.addEventListener('fullscreenchange',()=>{if(requireFullscreen && !document.fullscreenElement)logEvent('fullscreen_exit','Student exited fullscreen');});
    window.addEventListener('offline',()=>logEvent('offline','Network connection lost'));
    window.addEventListener('online',()=>logEvent('online','Network connection restored'));
    window.addEventListener('beforeunload',()=>{if(!intentionalSubmit)logEvent('page_leave','Attempted to leave or refresh exam page');});
  }
  if (blockCopy) {
    ['copy','cut','paste'].forEach(evt=>document.addEventListener(evt,e=>{e.preventDefault();logEvent(evt==='paste'?'paste_attempt':'copy_attempt',`${evt} blocked`);},{capture:true}));
    document.addEventListener('contextmenu',e=>{e.preventDefault();logEvent('context_menu','Right-click menu blocked');});
  }
  const gate=document.getElementById('secureGate');document.getElementById('enterExam')?.addEventListener('click',async()=>{try{await document.documentElement.requestFullscreen();gate.remove();logEvent('fullscreen_enter','Entered fullscreen exam mode');}catch(e){logEvent('fullscreen_denied','Browser denied fullscreen request');gate.querySelector('p').textContent='Fullscreen could not be entered. Check your browser permissions and try again.';}});
})();
