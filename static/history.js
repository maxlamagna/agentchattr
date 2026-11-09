/* Bounded retained-history inspection; live chat keeps processing underneath. */
(() => {
    let dialog, form, rows, status, more, back, searchButton, note;
    let generation = 0, pending, page = null, query = {}, next = null;
    let available = false;
    const node = (tag, text, className) => {
        const el = document.createElement(tag);
        if (text !== undefined) el.textContent = text;
        if (className) el.className = className;
        return el;
    };
    const invalidate = () => { generation++; pending?.abort(); };
    const headers = () => ({'Content-Type': 'application/json', 'X-Session-Token': SESSION_TOKEN});
    const dateBound = (value, nextDay) => {
        if (!value) return null;
        const [year, month, day] = value.split('-').map(Number);
        return new Date(year, month - 1, day + (nextDay ? 1 : 0)).getTime() / 1000;
    };
    function close() {
        invalidate(); dialog.close(); rows.replaceChildren(); page = null;
        document.getElementById('timeline').scrollTop = document.getElementById('timeline').scrollHeight;
        document.getElementById('input').focus();
    }
    function show() {
        invalidate();
        if (!dialog.open) dialog.showModal();
    }
    async function fetchView(path, body) {
        invalidate();
        const current = generation;
        pending = new AbortController();
        status.textContent = 'Loading…'; rows.replaceChildren(); more.hidden = back.hidden = true;
        try {
            const response = await fetch(path, {method: body ? 'POST' : 'GET', headers: headers(),
                ...(body ? {body: JSON.stringify(body)} : {}), signal: pending.signal});
            if (!response.ok) throw new Error(response.status === 404 ? 'History or message unavailable. The server may need a restart.' : 'Could not load history. Please try again.');
            const data = await response.json();
            return current === generation && dialog.open ? data : null;
        } catch (error) {
            if (current === generation && error.name !== 'AbortError') {
                status.textContent = error.message;
                back.hidden = !page;
            }
            return null;
        }
    }
    function render(messages, target) {
        rows.replaceChildren();
        for (const msg of messages) {
            const article = node('article', undefined, 'history-row');
            article.dataset.historyId = msg.id;
            if (msg.id === target) article.classList.add('history-target');
            const meta = `${msg.sender} · ${new Date(msg.timestamp * 1000).toLocaleString()} · #${msg.channel || 'general'} · ${msg.id}`;
            if (target === undefined) {
                const select = node('button', meta, 'history-result');
                select.type = 'button'; select.onclick = () => context(msg.id);
                article.append(select);
            } else {
                article.append(node('div', meta, 'history-meta'));
                if (msg.reply_to !== undefined && msg.reply_to !== null) {
                    const parent = node('button', `View reply to #${msg.reply_to}`, 'history-parent');
                    parent.type = 'button'; parent.onclick = () => context(msg.reply_to);
                    article.append(parent);
                }
            }
            article.append(node('div', msg.text, 'history-text'));
            rows.append(article);
        }
        if (target !== undefined) rows.querySelector('.history-target')?.scrollIntoView({block: 'center'});
        else rows.scrollTop = 0;
    }
    function results() {
        invalidate();
        if (!page) return;
        render(page.messages);
        status.textContent = page.messages.length ? `${page.messages.length} results · newest first` : 'No matching messages.';
        back.hidden = true; more.hidden = page.next_before_id === null;
        next = page.next_before_id;
    }
    async function search(before = null) {
        const data = await fetchView('/api/history/search', {...query, before_id: before, limit: 25});
        if (data) { page = data; results(); }
    }
    async function context(id) {
        show();
        const data = await fetchView(`/api/history/context/${Number(id)}`);
        if (!data) return;
        render(data.messages, data.target_id);
        status.textContent = `Conversation in #${data.channel} · message ${data.target_id}`;
        back.hidden = !page;
    }
    function open() {
        show(); page = null;
        const select = form.elements.channel;
        const selected = select.value;
        select.replaceChildren(node('option', 'All channels'));
        select.firstChild.value = '';
        for (const channel of window.channelList) {
            const option = node('option', `#${channel}`); option.value = channel; select.append(option);
        }
        select.value = selected;
        if (select.selectedIndex < 0) select.value = '';
        submit(); form.elements.text.focus();
    }
    function submit() {
        query = {text: form.elements.text.value, channel: form.elements.channel.value,
            sender: form.elements.sender.value.trim(),
            from_ts: dateBound(form.elements.from.value, false), to_ts: dateBound(form.elements.to.value, true)};
        page = null; search();
    }
    document.addEventListener('DOMContentLoaded', async () => {
        dialog = node('dialog', undefined, 'history-dialog');
        dialog.setAttribute('aria-labelledby', 'history-heading');
        // Static controls only. Every returned string is rendered with textContent.
        dialog.innerHTML = `<header><h2 id="history-heading">Search retained history</h2><button type="button" class="history-close">Back to latest</button></header>
            <form class="history-form"><label>Text<input name="text" type="search" placeholder="Find words in any retained message"></label>
            <label>Channel<select name="channel"><option value="">All channels</option></select></label>
            <label>Sender<input name="sender" placeholder="Any sender"></label>
            <label>From<input name="from" type="date"></label><label>Through<input name="to" type="date"></label>
            <button type="submit">Search</button></form>
            <nav><span class="history-status" role="status"></span><button type="button" class="history-back" hidden>Back to results</button></nav>
            <section class="history-rows" aria-label="History messages"></section>
            <footer><button type="button" class="history-more" hidden>More results</button><span>Live chat continues while you browse. Dates use your local time.</span></footer>`;
        document.body.append(dialog);
        form = dialog.querySelector('form'); rows = dialog.querySelector('.history-rows');
        status = dialog.querySelector('.history-status'); more = dialog.querySelector('.history-more');
        back = dialog.querySelector('.history-back'); searchButton = document.getElementById('history-search');
        note = document.getElementById('history-note');
        form.onsubmit = event => { event.preventDefault(); submit(); };
        dialog.querySelector('.history-close').onclick = close;
        dialog.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                event.preventDefault(); event.stopPropagation(); close();
            }
        });
        dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
        back.onclick = results; more.onclick = () => search(next); searchButton.onclick = open;
        window.chatHistory = {open, context, dateBound, get available() { return available; }};
        try {
            const response = await fetch('/api/history/search', {method: 'POST', headers: headers(), body: JSON.stringify({limit: 1})});
            available = response.ok;
            searchButton.hidden = !available;
            if (response.status === 404) {
                note.textContent = 'History search available after server restart'; note.hidden = false;
            }
        } catch (_) { /* Live chat remains usable if the capability probe fails. */ }
    });
})();
