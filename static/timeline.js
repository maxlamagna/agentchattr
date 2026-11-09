/* A bounded, contiguous ordinary conversation window for each channel. */
(() => {
    const CAP = 500, PAGE = 100;
    const states = new Map(), previews = new Map(), waiting = new Set();
    let timeline, box, earlier, newer, latest, previewTimer;
    let moving = false, moveGeneration = 0, lastTop = 0, previewEpoch = 0, previewBusy = false, pageAvailable = true, parentsAvailable = true;
    const state = (ch = activeChannel) => {
        if (!states.has(ch)) states.set(ch, {before: true, after: false, generation: 0,
            pending: null, anchor: null, error: '', unread: 0, liveMax: -1, low: null, high: null});
        return states.get(ch);
    };
    const nodes = ch => [...box.querySelectorAll('.message[data-id]')]
        .filter(el => (el.dataset.channel || 'general') === ch);
    const headers = () => ({'Content-Type': 'application/json', 'X-Session-Token': SESSION_TOKEN});
    const excerpt = msg => ({id: msg.id, sender: msg.sender, text: (msg.text || '').slice(0, 81),
        channel: msg.channel || 'general'});
    function remember(msg) {
        if (!Number.isSafeInteger(msg.id)) return;
        previews.delete(msg.id); previews.set(msg.id, excerpt(msg));
        while (previews.size > 1000) previews.delete(previews.keys().next().value);
    }
    function quote(id) {
        const msg = previews.get(id);
        if (!msg || msg.missing) return `<button type="button" class="reply-quote" data-parent-id="${id}" onclick="scrollToMessage(${id})">${msg?.missing ? 'Original message unavailable' : `View reply to #${id}`}</button>`;
        const text = msg.text.length > 80 ? msg.text.slice(0, 80) + '...' : msg.text;
        return `<div class="reply-quote" data-parent-id="${id}" onclick="scrollToMessage(${id})"><span class="reply-sender" style="color: ${getColor(msg.sender)}">${escapeHtml(msg.sender)}</span> ${escapeHtml(text)}</div>`;
    }
    function updateQuotes(id, missing = false) {
        for (const el of box.querySelectorAll(`.reply-quote[data-parent-id="${id}"]`)) {
            if (missing) { el.textContent = 'Original message unavailable'; el.dataset.loaded = 'yes'; }
            else {
                const wrapper = document.createElement('div'); wrapper.innerHTML = quote(id);
                el.replaceWith(wrapper.firstElementChild);
            }
        }
    }
    function observeQuotes() {
        if (!parentsAvailable) return;
        for (const el of box.querySelectorAll('.reply-quote[data-parent-id]')) {
            const id = Number(el.dataset.parentId);
            if (!el.querySelector('.reply-sender') && !el.dataset.loaded) {
                if (previews.has(id)) updateQuotes(id, previews.get(id).missing);
                else waiting.add(id);
            }
        }
        clearTimeout(previewTimer); previewTimer = setTimeout(fetchPreviews, 25);
    }
    async function fetchPreviews() {
        if (previewBusy || !waiting.size) return;
        const ids = [...waiting].slice(0, 50), epoch = previewEpoch;
        previewBusy = true;
        try {
            const response = await fetch('/api/history/parents', {method: 'POST', headers: headers(),
                body: JSON.stringify({ids})});
            if (response.status === 404) { parentsAvailable = false; waiting.clear(); return; }
            if (!response.ok) throw new Error('Reply preview unavailable');
            const data = await response.json();
            if (epoch !== previewEpoch) return;
            data.messages.forEach(remember);
            ids.forEach(id => {
                if (!previews.has(id)) { previews.set(id, {id, missing: true}); while (previews.size > 1000) previews.delete(previews.keys().next().value); }
                updateQuotes(id, previews.get(id)?.missing);
            });
        } catch (_) {
            // The quote stays clickable; another render can retry, without a loop.
        } finally {
            ids.forEach(id => waiting.delete(id));
            previewBusy = false;
            if (waiting.size) previewTimer = setTimeout(fetchPreviews, 25);
        }
    }
    function capture(ch = activeChannel) {
        const top = timeline.getBoundingClientRect().top;
        const el = nodes(ch).find(row => row.getBoundingClientRect().bottom > top && row.getBoundingClientRect().height);
        return el ? {id: Number(el.dataset.id), offset: el.getBoundingClientRect().top - top} : null;
    }
    function restore(anchor) {
        if (!anchor) return;
        const el = document.getElementById(`message-${anchor.id}`) ||
            nodes(activeChannel).find(row => Number(row.dataset.id) > anchor.id) || nodes(activeChannel).at(-1);
        if (!el || el.dataset.channel !== activeChannel) return;
        timeline.scrollTop += el.getBoundingClientRect().top - timeline.getBoundingClientRect().top - anchor.offset;
    }
    function quiet(fn) {
        const current = ++moveGeneration;
        timeline.style.overflowAnchor = 'none';
        moving = true; fn(); lastTop = timeline.scrollTop;
        requestAnimationFrame(() => {
            if (current !== moveGeneration) return;
            timeline.style.overflowAnchor = 'auto';
            lastTop = timeline.scrollTop; moving = false; controls();
        });
    }
    function flatten(ch) {
        for (const group of box.querySelectorAll('.job-group')) {
            const rows = [...group.querySelectorAll('.message')];
            if (rows.some(el => el.dataset.channel === ch)) {
                rows.forEach(el => { el.dataset.jobExpanded = group.classList.contains('expanded') ? 'yes' : ''; });
                group.replaceWith(...rows);
            }
        }
    }
    function remove(el) {
        el.remove();
    }
    function tidy(ch, direction = 'after', force = false) {
        flatten(ch);
        let rows = nodes(ch);
        const extra = Math.max(0, rows.length - CAP);
        const readingLatest = !state(ch).after && (ch === activeChannel ? !autoScroll : state(ch).anchor);
        if (extra && (force || direction === 'before' || !readingLatest)) {
            const removed = direction === 'before' ? rows.slice(-extra) : rows.slice(0, extra);
            removed.forEach(remove);
            if (direction === 'before') state(ch).after = true;
            else state(ch).before = true;
        }
        for (const divider of box.querySelectorAll('.date-divider')) {
            if (divider.dataset.channel === ch) divider.remove();
        }
        rows = nodes(ch);
        let date;
        for (const el of rows) {
            if (el.dataset.date !== date) {
                date = el.dataset.date;
                const divider = document.createElement('div'); divider.className = 'date-divider';
                divider.dataset.channel = ch;
                const label = document.createElement('span'); label.textContent = formatDateDivider(date); divider.append(label);
                if (ch !== activeChannel) divider.style.display = 'none';
                el.before(divider);
            }
        }
        for (const el of rows.filter(el => el.classList.contains('job-breadcrumb'))) {
            window._collapseJobBreadcrumbs?.(box, el);
        }
        for (const group of box.querySelectorAll('.job-group')) {
            group.dataset.channel = group.querySelector('.message')?.dataset.channel || 'general';
            group.style.display = group.dataset.channel === activeChannel ? '' : 'none';
            if (group.querySelector('[data-job-expanded="yes"]')) group.classList.add('expanded');
        }
        const ids = rows.map(el => Number(el.dataset.id));
        if (ids.length) { state(ch).low = Math.min(...ids); state(ch).high = Math.max(...ids); }
        lastMessageDates[ch] = date;
        observeQuotes();
    }
    function controls() {
        if (!earlier) return;
        const s = state();
        earlier.hidden = historyLoading || (!s.before && !s.error);
        earlier.textContent = !pageAvailable ? 'Earlier history available after server restart' : s.pending ? 'Loading conversation…' : s.error || 'Load earlier';
        earlier.disabled = !!s.pending || moving || !pageAvailable;
        newer.hidden = !s.after; newer.disabled = !!s.pending || moving;
        newer.textContent = s.pending ? 'Loading conversation…' : s.direction === 'after' && s.error ? s.error : 'Load newer';
        latest.hidden = !s.after;
        latest.textContent = unreadCount ? `Back to latest (${unreadCount} new)` : 'Back to latest';
    }
    function cancel(s) { s.generation++; s.pending?.abort(); s.pending = null; }
    async function load(direction, ch = activeChannel) {
        const s = state(ch);
        if (direction === 'latest' && s.pending) cancel(s);
        if (historyLoading || s.pending || !pageAvailable) return;
        if ((direction === 'before' && !s.before) || (direction === 'after' && !s.after)) return;
        const generation = ++s.generation;
        const controller = new AbortController(); s.pending = controller; s.direction = direction;
        s.arrivals = []; s.error = ''; controls();
        const body = {channel: ch, limit: direction === 'latest' ? CAP : PAGE};
        if (direction === 'before' && s.low !== null) body.before_id = s.low;
        if (direction === 'after' && s.high !== null) body.after_id = s.high;
        try {
            const response = await fetch('/api/history/page', {method: 'POST', headers: headers(),
                body: JSON.stringify(body), signal: controller.signal});
            if (response.status === 404) { pageAvailable = false; return; }
            if (!response.ok) throw new Error('Could not load conversation — retry');
            const data = await response.json();
            if (generation !== s.generation || states.get(ch) !== s) return;
            const atLatest = direction === 'latest';
            const anchor = ch === activeChannel ? capture(ch) : s.anchor;
            if (!atLatest && anchor) {
                const ids = nodes(ch).map(el => Number(el.dataset.id));
                const incoming = data.messages.map(m => m.id).filter(id => !ids.includes(id));
                const combined = direction === 'before' ? [...incoming, ...ids] : [...ids, ...incoming];
                const extra = Math.max(0, combined.length - CAP);
                const removed = direction === 'before' ? combined.slice(-extra || combined.length) : combined.slice(0, extra);
                if (removed.includes(anchor.id)) return; // The reader moved while the page was in flight.
            }
            quiet(() => {
                flatten(ch);
                if (atLatest) {
                    nodes(ch).forEach(remove);
                    s.before = data.has_before; s.after = data.has_after;
                } else if (direction === 'before') s.before = data.has_before;
                else s.after = data.has_after;
                const fragment = document.createDocumentFragment();
                const lastReturned = data.messages.at(-1)?.id ?? -1;
                const messages = atLatest ? [...data.messages, ...s.arrivals.filter(m => m.id > lastReturned)] : data.messages;
                messages.forEach(remember);
                for (const msg of messages) appendMessage(msg, {history: true, container: fragment, windowRender: true});
                if (direction === 'before') box.insertBefore(fragment, nodes(ch)[0] || null);
                else box.append(fragment);
                const last = messages.at(-1)?.id ?? -1;
                if ((direction === 'latest' || direction === 'after') && s.liveMax > last) s.after = true;
                tidy(ch, direction, true);
                controls(); // Boundary controls affect the anchor's vertical position.
                if (ch === activeChannel) {
                    autoScroll = atLatest && !s.after;
                    if (atLatest) {
                        timeline.scrollTop = timeline.scrollHeight;
                        s.unread = 0; unreadCount = 0; channelUnread[ch] = 0; updateScrollAnchor(); renderChannelTabs();
                    } else restore(anchor);
                }
                s.anchor = atLatest ? null : anchor;
            });
        } catch (error) {
            if (generation === s.generation && error.name !== 'AbortError') s.error = 'Could not load conversation — retry';
        } finally {
            if (generation === s.generation) { s.pending = null; s.arrivals = []; }
            controls();
        }
    }
    function hold(msg, options) {
        const ch = msg.channel || 'general', s = state(ch);
        if (options.windowRender) return false;
        s.liveMax = Math.max(s.liveMax, msg.id);
        if (options.history) return false;
        if (s.pending && s.direction === 'latest') {
            s.arrivals.push(msg);
            if (s.arrivals.length > CAP) s.arrivals.shift();
        }
        if (s.after) {
            s.after = true; if (ch === activeChannel) autoScroll = false;
            return true;
        }
        return false;
    }
    function appended(msg, options) {
        if (options.container || options.windowRender) return;
        if (options.history) { queueMicrotask(() => { tidy(msg.channel || 'general'); controls(); }); return; }
        tidy(msg.channel || 'general'); controls();
    }
    function replayStart() {
        if (!box) return;
        previewEpoch++; waiting.clear(); previews.clear();
        for (const [ch, s] of states) {
            cancel(s); flatten(ch); nodes(ch).forEach(remove);
        }
        states.clear(); autoScroll = true;
    }
    function replayDone() {
        for (const ch of channelList) {
            tidy(ch);
        }
        controls();
    }
    function event(event) {
        if (event.type === 'edit' || event.type === 'message_update') {
            if (event.message) { remember(event.message); updateQuotes(event.message.id); }
        } else if (event.type === 'delete') {
            for (const s of states.values()) if (event.ids.includes(s.liveMax)) s.liveMax = -1;
            for (const id of event.ids) {
                previews.delete(id); previews.set(id, {id, missing: true}); updateQuotes(id, true);
                while (previews.size > 1000) previews.delete(previews.keys().next().value);
            }
        } else if (event.type === 'clear') {
            previewEpoch++; previews.clear(); waiting.clear();
            for (const [ch, s] of states) if (!event.channel || event.channel === ch) {
                cancel(s); s.before = s.after = false; s.low = s.high = null; s.liveMax = -1; s.anchor = null; s.unread = 0;
            }
        } else if (event.type === 'channel_renamed') {
            const s = states.get(event.old_name);
            if (s) { cancel(s); states.delete(event.old_name); states.set(event.new_name, s); }
            for (const msg of previews.values()) if (msg.channel === event.old_name) msg.channel = event.new_name;
        } else if (event.type === 'agent_renamed') {
            for (const msg of previews.values()) if (msg.sender === event.old_name) {
                msg.sender = event.new_name; updateQuotes(msg.id);
            }
        }
        if (['edit', 'message_update', 'delete', 'clear'].includes(event.type)) {
            previewEpoch++;
            for (const s of states.values()) cancel(s);
        }
        queueMicrotask(controls);
    }
    function save(ch) { state(ch).anchor = !autoScroll || state(ch).after ? capture(ch) : null; }
    function activate(ch) {
        const s = state(ch);
        autoScroll = !s.anchor && !s.after;
        if (autoScroll) { s.unread = 0; channelUnread[ch] = 0; }
        unreadCount = s.unread;
        quiet(() => { controls(); if (s.anchor) restore(s.anchor); else if (!s.after) timeline.scrollTop = timeline.scrollHeight; });
        if (!s.anchor && s.after) load('latest', ch);
        controls(); observeQuotes();
        return true;
    }
    function checkScroll(direction) {
        if (moving || historyLoading || state().pending || state().error) return;
        if (direction < 0 && timeline.scrollTop < 180) load('before');
        if (direction > 0 && timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 180) load('after');
    }
    document.addEventListener('DOMContentLoaded', () => {
        timeline = document.getElementById('timeline'); box = document.getElementById('messages');
        const button = (id, parent, before) => {
            const el = document.createElement('button'); el.id = id; el.type = 'button';
            el.className = 'timeline-history-control'; el.hidden = true;
            parent.insertBefore(el, before || null); return el;
        };
        earlier = button('timeline-earlier', timeline, box);
        newer = button('timeline-newer', timeline, box.nextSibling);
        latest = button('timeline-latest', document.getElementById('content-area'));
        earlier.onclick = () => { state().error = ''; load('before'); };
        newer.onclick = () => { state().error = ''; load('after'); };
        latest.onclick = () => load('latest');
        timeline.addEventListener('scroll', () => {
            const direction = timeline.scrollTop - lastTop; lastTop = timeline.scrollTop;
            checkScroll(direction);
        }, {passive: true});
        timeline.addEventListener('wheel', e => checkScroll(e.deltaY), {passive: true});
    });
    window.chatTimeline = {remember, quote, hold, appended, replayStart, replayDone, event, save, activate,
        latest: () => load('latest'), detached: (ch = activeChannel) => state(ch).after, get moving() { return moving; },
        arrival(msg) { const ch = msg.channel || 'general'; if (ch === activeChannel ? !autoScroll : !['join', 'leave'].includes(msg.type)) state(ch).unread++; },
        readLatest() {
            state().unread = 0; channelUnread[activeChannel] = 0;
            if (nodes(activeChannel).length > CAP) tidy(activeChannel, 'after', true);
            controls();
        },
        settle(messages) { for (const ch of new Set(messages.map(m => m.channel || 'general'))) tidy(ch); controls(); }};
})();
