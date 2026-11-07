'use strict';
const content = document.querySelector('#content');
const retry = document.querySelector('#retry');
let selected = 'schedule';
let requestNumber = 0;
async function load(path) {
  selected = path;
  const version = ++requestNumber;
  content.setAttribute('aria-busy', 'true');
  content.replaceChildren();
  content.textContent = 'Загружаем данные…';
  retry.hidden = true;
  document.querySelectorAll('nav button').forEach(button => {
    if (button.dataset.path === path) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  try {
    const initData = window.WebApp?.initData;
    if (!initData) throw new Error('Открой приложение из MAX, чтобы увидеть данные своей группы.');
    const response = await fetch(`/api/${path}`, {headers: {'X-Max-Init-Data': initData}, cache: 'no-store', credentials: 'omit'});
    const data = await response.json();
    if (version !== requestNumber) return;
    if (!response.ok) throw new Error(data.error || 'Не удалось загрузить данные.');
    document.querySelector('#group').textContent = data.group ? `Твоя группа · ${data.group}` : 'Выбери группу в чате с ботом';
    document.querySelector('#demo').hidden = !data.demo;
    content.replaceChildren(...data.items.map(text => {
      const card = document.createElement('article');
      card.textContent = text;
      return card;
    }));
  } catch (error) {
    if (version !== requestNumber) return;
    content.textContent = error.message || 'Проверь соединение и попробуй ещё раз.';
    retry.hidden = false;
  } finally {
    if (version === requestNumber) content.setAttribute('aria-busy', 'false');
  }
}
document.querySelectorAll('nav button').forEach(button => button.addEventListener('click', () => load(button.dataset.path)));
retry.addEventListener('click', () => load(selected));
window.WebApp?.ready?.();
load(selected);
