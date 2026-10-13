'use strict';

const ui = Object.fromEntries(['mode', 'mode-note', 'document-count', 'documents', 'upload-button', 'file-input', 'upload-status', 'question-form', 'question', 'question-count', 'task-title', 'ask-button', 'global-status', 'answer', 'citations', 'run-meta', 'steps', 'step-count', 'approvals', 'approval-count', 'tasks', 'task-count', 'conversations', 'new-conversation', 'conversation-title', 'turn-count', 'turns'].map(id => [id, document.getElementById(id)]));
let csrfToken = '';
let asking = false;
let conversationId = localStorage.getItem('evidence-conversation-id') || '';

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function setStatus(element, message, error = false) {
  element.textContent = message;
  element.classList.toggle('error', error);
}

async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      credentials: 'same-origin'
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || '请求失败，请刷新后重试。');
    return value;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('等待超时，请稍后重试并检查服务状态。');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function renderDocuments(documents) {
  ui['documents'].replaceChildren();
  ui['document-count'].textContent = documents.length;
  if (!documents.length) {
    ui['documents'].append(node('li', 'empty-line', '还没有资料，先导入一个文本文件。'));
    return;
  }
  for (const document of documents) {
    const item = node('li');
    const text = node('div');
    text.append(node('strong', '', document.filename), node('small', '', `${document.chunks} 个片段 · ${document.characters} 字`));
    item.append(node('span', 'doc-icon', document.filename.toLowerCase().endsWith('.md') ? 'MD' : 'TXT'), text);
    ui['documents'].append(item);
  }
}

function renderTasks(tasks) {
  ui['tasks'].replaceChildren();
  ui['task-count'].textContent = tasks.length;
  if (!tasks.length) ui['tasks'].append(node('li', 'empty-line', '确认后创建的任务会保存在这里。'));
  for (const task of tasks) {
    const item = node('li', '', task.title);
    item.append(node('small', '', new Date(task.created_at).toLocaleString('zh-CN')));
    ui['tasks'].append(item);
  }
}

function renderApprovals(approvals) {
  ui['approvals'].replaceChildren();
  ui['approval-count'].textContent = approvals.length;
  if (!approvals.length) ui['approvals'].append(node('p', 'empty-line', '没有待确认操作。'));
  for (const approval of approvals) {
    const card = node('div', 'approval-card');
    card.append(node('p', '', approval.title), node('small', '', '点击“确认创建”将此标题写入任务列表。'));
    const actions = node('div', 'approval-actions');
    for (const [approved, label, className] of [[true, '确认创建', 'primary'], [false, '放弃', 'secondary']]) {
      const button = node('button', `button ${className}`, label);
      button.type = 'button';
      button.setAttribute('aria-label', `${label}：${approval.title}`);
      button.addEventListener('click', async () => {
        actions.querySelectorAll('button').forEach(control => { control.disabled = true; });
        try {
          await api(`/api/approvals/${encodeURIComponent(approval.id)}`, { approved, conversation_id: conversationId });
          setStatus(ui['global-status'], approved ? '任务已创建并保存。' : '已放弃该任务。');
          await refreshState();
        } catch (error) {
          setStatus(ui['global-status'], error.message, true);
          actions.querySelectorAll('button').forEach(control => { control.disabled = false; });
        }
      });
      actions.append(button);
    }
    card.append(actions);
    ui['approvals'].append(card);
  }
}

async function refreshState() {
  const state = await api('/api/state');
  csrfToken = state.csrf_token;
  renderDocuments(state.documents);
  renderTasks(state.tasks);
  renderConversations(state.conversations);
  renderApprovals(state.pending_approvals.filter(item => item.conversation_id === conversationId));
  const modelOption = ui['mode'].querySelector('option[value="ollama"]');
  modelOption.disabled = !state.ollama_available;
  modelOption.textContent = state.ollama_available ? '本地 Ollama（已配置）' : '本地 Ollama（未配置）';
  if (!state.ollama_available) ui['mode'].value = 'demo';
}

function renderConversations(conversations) {
  ui['conversations'].replaceChildren();
  if (!conversations.some(item => item.id === conversationId)) conversationId = '';
  for (const item of conversations) {
    const row = node('li');
    const button = node('button', `conversation-button${item.id === conversationId ? ' active' : ''}`, item.title);
    button.type = 'button';
    button.append(node('small', '', `${item.turns} 轮 · ${new Date(item.created_at).toLocaleDateString('zh-CN')}`));
    button.addEventListener('click', async () => {
      if (asking) return;
      try {
        conversationId = item.id;
        localStorage.setItem('evidence-conversation-id', conversationId);
        await loadConversation();
        await refreshState();
      } catch (error) { setStatus(ui['global-status'], error.message, true); }
    });
    row.append(button);
    ui['conversations'].append(row);
  }
  if (!conversations.length) ui['conversations'].append(node('li', 'empty-line', '提问后会自动保存会话。'));
}

async function loadConversation() {
  if (!conversationId) return;
  const thread = await api(`/api/conversations/${encodeURIComponent(conversationId)}`);
  ui['conversation-title'].textContent = thread.title;
  ui['turn-count'].textContent = `${thread.runs.length} 轮`;
  ui['turns'].replaceChildren();
  for (const [index, run] of thread.runs.entries()) {
    const row = node('li');
    const button = node('button', 'turn-button', `第 ${index + 1} 轮 · ${run.question}`);
    button.type = 'button';
    button.addEventListener('click', () => renderRun(run));
    row.append(button, node('p', '', run.answer.slice(0, 220)));
    ui['turns'].append(row);
  }
  if (thread.runs.length) renderRun(thread.runs[thread.runs.length - 1]);
}

ui['new-conversation'].addEventListener('click', () => {
  if (asking) return;
  conversationId = '';
  localStorage.removeItem('evidence-conversation-id');
  ui['conversation-title'].textContent = '新会话';
  ui['turn-count'].textContent = '0 轮';
  ui['turns'].replaceChildren();
  ui['answer'].textContent = '先提出一个问题';
  ui['citations'].replaceChildren();
  ui['steps'].replaceChildren();
  renderApprovals([]);
  ui['step-count'].textContent = '0';
  ui['run-meta'].textContent = '';
  ui['question'].value = '';
  ui['question-count'].textContent = '0 / 2000';
  setStatus(ui['global-status'], '已开启新会话，历史会话仍可从左侧查看。');
  ui['question'].focus();
});

const toolLabels = { knowledge_search: '检索资料', task_list: '读取任务', task_create: '等待你的确认', model_plan: '模型规划 / 回答' };
function renderRun(run) {
  ui['answer'].textContent = run.answer;
  ui['run-meta'].textContent = `${run.mode === 'demo' ? '演示' : 'Ollama'} · ${run.elapsed_ms} ms`;
  ui['citations'].replaceChildren();
  for (const [index, citation] of run.citations.entries()) {
    const details = node('details', 'citation');
    details.append(node('summary', '', `[${index + 1}] ${citation.filename} · 第 ${citation.line_start}–${citation.line_end} 行`), node('p', '', citation.text), node('small', '', `BM25 关键词匹配分数 ${citation.score}；分数不代表答案正确率。`));
    ui['citations'].append(details);
  }
  ui['steps'].replaceChildren();
  ui['step-count'].textContent = run.steps.length;
  for (const step of run.steps) {
    const item = node('li');
    const pending = step.status === 'awaiting_confirmation';
    item.append(node('span', `step-mark${pending ? ' pending' : ''}`, pending ? '!' : '✓'), node('strong', '', toolLabels[step.tool] || step.tool), node('p', '', step.summary), node('small', '', `${step.tool} · ${step.duration_ms} ms`));
    if (step.arguments !== undefined || step.observation !== undefined) {
      const detail = node('details', 'trace-detail');
      detail.append(node('summary', '', '查看输入与工具反馈'), node('pre', '', JSON.stringify({ call_id: step.call_id, arguments: step.arguments, observation: step.observation }, null, 2)));
      item.append(detail);
    }
    ui['steps'].append(item);
  }
}

ui['upload-button'].addEventListener('click', () => ui['file-input'].click());
ui['file-input'].addEventListener('change', async () => {
  const file = ui['file-input'].files[0];
  if (!file) return;
  if (!/\.(md|txt)$/i.test(file.name) || file.size > 256 * 1024) {
    setStatus(ui['upload-status'], '请选择不超过 256 KiB 的 .md 或 .txt 文件。', true);
    ui['file-input'].value = '';
    return;
  }
  ui['upload-button'].disabled = true;
  setStatus(ui['upload-status'], '正在导入资料…');
  try {
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
    const result = await api('/api/documents', { filename: file.name, content });
    setStatus(ui['upload-status'], `已导入 ${result.document.filename}。`);
    await refreshState();
  } catch (error) {
    setStatus(ui['upload-status'], error instanceof TypeError ? '无法读取 UTF-8 文本，请转换编码后重试。' : error.message, true);
  } finally {
    ui['upload-button'].disabled = false;
    ui['file-input'].value = '';
  }
});
ui['question'].addEventListener('input', () => { ui['question-count'].textContent = `${ui['question'].value.length} / 2000`; });
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  ui['question'].value = button.dataset.question;
  ui['question'].dispatchEvent(new Event('input'));
  ui['question'].focus();
}));
ui['mode'].addEventListener('change', () => {
  ui['mode-note'].querySelector('p').textContent = ui['mode'].value === 'demo' ? '演示模式按固定规则检索与编排，不是大模型推理。' : '本地模型自主选择白名单工具，读取反馈后继续回答；创建待办仅提出请求，仍需你单独确认。';
});
ui['question-form'].addEventListener('submit', async event => {
  event.preventDefault();
  if (asking) return;
  const question = ui['question'].value.trim();
  if (!question) { ui['question'].focus(); return; }
  asking = true;
  ui['ask-button'].disabled = true;
  ui['ask-button'].textContent = '正在检索…';
  ui['mode'].disabled = true;
  ui['new-conversation'].disabled = true;
  setStatus(ui['global-status'], '正在读取资料与执行计划…');
  const body = { question, mode: ui['mode'].value };
  if (conversationId) body.conversation_id = conversationId;
  const title = ui['task-title'].value.trim();
  if (title) body.draft_task_title = title;
  try {
    const run = await api('/api/runs', body);
    conversationId = run.conversation_id;
    localStorage.setItem('evidence-conversation-id', conversationId);
    renderRun(run);
    setStatus(ui['global-status'], run.pending_approval ? '回答已完成，待办还需要你确认。' : '回答已完成，可展开引用查看原文。');
    await refreshState();
    await loadConversation();
    ui['task-title'].value = '';
  } catch (error) {
    setStatus(ui['global-status'], error.message, true);
  } finally {
    asking = false;
    ui['ask-button'].disabled = false;
    ui['ask-button'].textContent = '检索并回答 ↗';
    ui['mode'].disabled = false;
    ui['new-conversation'].disabled = false;
  }
});
refreshState().then(loadConversation).catch(error => {
  setStatus(ui['global-status'], `无法连接本地服务：${error.message}`, true);
  ui['ask-button'].disabled = true;
  ui['upload-button'].disabled = true;
});
