/* Actual timeline state machine, minimal DOM adapter. Browser layout is tested separately. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('deleted newest-seen ID does not prevent returning to retained latest', async () => {
    const h = harness(); await h.detach();
    h.context.chatTimeline.event({type: 'delete', ids: [599]});
    h.context.fetch = async () => h.response({messages: h.rows(99, 598), has_before: true, has_after: false});
    await h.context.chatTimeline.latest();
    assert.equal(h.context.chatTimeline.detached(), false);
    assert.equal(h.context.chatTimeline.hold({id: 600, channel: 'general'}, {}), false);
});

test('deleting an unrelated ID preserves the newer live-arrival marker', async () => {
    const h = harness(); await h.detach();
    h.context.chatTimeline.hold({id: 700, channel: 'general'}, {});
    h.context.chatTimeline.event({type: 'delete', ids: [599]});
    h.timeline.scrollTop = 24000;
    h.context.fetch = async () => h.response({messages: h.rows(500, 598), has_before: true, has_after: false});
    h.context.document.getElementById('timeline-newer').onclick();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.context.chatTimeline.detached(), true);
});

test('readLatest preserves date divider elements when at or below the cap', () => {
    for (const count of [100, 500]) {
        const h = harness(); h.seed(0, count - 1);
        const dividers = h.box.querySelectorAll('.date-divider');
        assert.ok(dividers.length);
        h.context.chatTimeline.readLatest();
        const after = h.box.querySelectorAll('.date-divider');
        assert.equal(after.length, dividers.length);
        dividers.forEach((el, i) => assert.ok(after[i] === el, 'existing divider identity changed'));
    }
});

function harness() {
    const callbacks = [];
    class Element {
        constructor() {
            this.children = []; this.dataset = {}; this.style = {}; this.className = ''; this.id = '';
            this.scrollTop = 0; this.clientHeight = 300;
            this.classList = {contains: cls => this.className.split(' ').includes(cls)};
            this.classList.add = (...classes) => { this.className = [...new Set([...this.className.split(' ').filter(Boolean), ...classes])].join(' '); };
        }
        append(...els) { for (const el of els) this.insertBefore(el, null); }
        appendChild(el) { this.append(el); return el; }
        insertBefore(el, before) {
            if (el.fragment) { [...el.children].forEach(child => this.insertBefore(child, before)); return; }
            el.remove(); const i = before ? this.children.indexOf(before) : -1;
            this.children.splice(i < 0 ? this.children.length : i, 0, el); el.parentElement = this;
        }
        remove() {
            if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1);
            this.parentElement = null;
        }
        before(el) { this.parentElement.insertBefore(el, this); }
        replaceWith(...els) { for (const el of els) this.before(el); this.remove(); }
        get nextSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1]; }
        get nextElementSibling() { return this.nextSibling; }
        get previousElementSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) - 1]; }
        querySelectorAll(selector) {
            const all = this.children.flatMap(el => [el, ...el.querySelectorAll('*')]);
            if (selector === '*') return all;
            if (selector === '[data-channel]') return all.filter(el => el.dataset.channel !== undefined);
            if (selector === '.message[data-id]') return all.filter(el => el.classList.contains('message') && el.dataset.id !== undefined);
            return all.filter(el => el.classList.contains(selector.slice(1)));
        }
        querySelector(selector) { return this.querySelectorAll(selector)[0]; }
        addEventListener() {}
        getBoundingClientRect() {
            if (this.id === 'timeline') return {top: 0, bottom: 300, height: 300};
            const siblings = this.parentElement?.children || [];
            const height = this.hidden ? 0 : this.classList.contains('message') ? 50 : 20;
            const parentTop = this.parentElement?.getBoundingClientRect().top || 0;
            const top = parentTop + siblings.slice(0, siblings.indexOf(this)).reduce((sum, el) => sum + (el.hidden ? 0 : el.classList.contains('message') ? 50 : 20), 0) - (this.parentElement === timeline ? timeline.scrollTop : 0);
            return {top, bottom: top + height, height};
        }
        get scrollHeight() { return this.children.reduce((sum, el) => sum + (el.id === 'messages' ? el.children.length * 50 : 20), 0); }
    }
    const root = new Element(), content = new Element(), timeline = new Element(), box = new Element();
    content.id = 'content-area'; timeline.id = 'timeline'; box.id = 'messages';
    root.append(content); content.append(timeline); timeline.append(box);
    const document = {createElement: () => new Element(),
        createDocumentFragment: () => Object.assign(new Element(), {fragment: true}),
        getElementById: id => [root, ...root.querySelectorAll('*')].find(el => el.id === id),
        addEventListener: (event, fn) => { if (event === 'DOMContentLoaded') callbacks.push(fn); }};
    const context = {document, activeChannel: 'general', channelList: ['general'], autoScroll: true,
        historyLoading: false, unreadCount: 0, channelUnread: {}, lastMessageDates: {}, SESSION_TOKEN: 'fixture',
        setTimeout, clearTimeout, queueMicrotask, requestAnimationFrame: fn => setTimeout(fn, 0),
        AbortController, console, renderChannelTabs() {}, updateScrollAnchor() {},
        formatDateDivider: date => date, getColor: () => 'blue',
        escapeHtml: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
        fetch: async () => { throw new Error('Unexpected fetch'); }};
    context.window = context;
    context.appendMessage = (msg, options) => {
        if (document.getElementById('message-' + msg.id)) return;
        const el = new Element(); el.id = 'message-' + msg.id; el.className = 'message';
        el.dataset = {id: String(msg.id), channel: msg.channel || 'general', date: 'Today'};
        (options?.container || box).append(el);
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/timeline.js'), 'utf8'), context);
    callbacks.forEach(fn => fn());
    function seed(first, last) {
        for (let id = first; id <= last; id++) {
            const msg = {id, sender: 'Max', text: 'Message ' + id, channel: 'general'};
            context.chatTimeline.remember(msg); context.chatTimeline.hold(msg, {history: true});
            context.appendMessage(msg);
        }
        context.chatTimeline.settle([{channel: 'general'}]);
    }
    const ids = () => box.querySelectorAll('.message[data-id]').map(el => Number(el.dataset.id));
    const response = data => ({ok: true, status: 200, json: async () => data});
    const rows = (first, last) => Array.from({length: last - first + 1}, (_, i) => ({id: first + i, sender: 'Max', text: 'Message', channel: 'general'}));
    async function detach() {
        seed(100, 599);
        context.fetch = async () => response({messages: rows(0, 99), has_before: false, has_after: true});
        document.getElementById('timeline-earlier').onclick();
        await new Promise(resolve => setTimeout(resolve, 0));
    }
    return {context, box, timeline, seed, ids, response, rows, detach};
}

test('live arrival in latest window stays scrollable without evicting the anchor', () => {
    const h = harness(); h.seed(0, 499); h.context.autoScroll = false;
    const msg = {id: 500, channel: 'general'};
    assert.equal(h.context.chatTimeline.hold(msg, {}), false);
    h.context.appendMessage(msg); h.context.chatTimeline.appended(msg, {});
    assert.equal(h.context.chatTimeline.detached(), false);
    assert.deepEqual(h.ids(), Array.from({length: 501}, (_, i) => i));
    h.context.chatTimeline.readLatest();
    assert.equal(h.ids().length, 500);
});

test('Back to latest merges arrivals after the snapshot and remains bounded', async () => {
    const h = harness(); h.seed(0, 499); h.context.autoScroll = false;
    let finish; h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = h.context.chatTimeline.latest();
    h.context.chatTimeline.hold({id: 500, sender: 'Max', channel: 'general', text: 'Race'}, {});
    finish(h.response({messages: h.rows(0, 499), has_before: false, has_after: false}));
    await pending;
    assert.deepEqual(h.ids(), Array.from({length: 500}, (_, i) => i + 1));
    assert.equal(h.context.chatTimeline.detached(), false);
});

test('clear invalidates a late response even when fetch ignores abort', async () => {
    const h = harness(); h.seed(0, 99);
    let finish; h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = h.context.chatTimeline.latest();
    h.context.chatTimeline.event({type: 'clear', channel: 'general'});
    h.box.querySelectorAll('.message[data-id]').forEach(el => el.remove());
    finish(h.response({messages: h.rows(0, 99), has_before: false, has_after: false}));
    await pending;
    assert.deepEqual(h.ids(), []);
});

test('network reconnect discards detached rows and cursors before replay', async () => {
    const h = harness(); await h.detach(); h.context.autoScroll = false;
    h.context.chatTimeline.hold({id: 700, channel: 'general'}, {});
    assert.equal(h.context.chatTimeline.detached(), true);
    h.context.chatTimeline.replayStart();
    assert.deepEqual(h.ids(), []);
    assert.equal(h.context.chatTimeline.detached(), false);
    assert.equal(h.context.autoScroll, true);
    assert.equal(h.context.chatTimeline.hold({id: 701, channel: 'general'}, {history: true}), false);
});

test('old server disables automatic paging after one 404', async () => {
    const h = harness(); h.seed(0, 99); let requests = 0;
    h.context.fetch = async () => { requests++; return {status: 404, ok: false}; };
    await h.context.chatTimeline.latest(); await h.context.chatTimeline.latest();
    assert.equal(requests, 1);
    assert.match(h.context.document.getElementById('timeline-earlier').textContent, /restart/);
});

test('old-server 404 never traps live arrivals before or after capability failure', async () => {
    const h = harness(); h.seed(0, 499); h.context.autoScroll = false;
    for (const id of [500, 501]) {
        const msg = {id, channel: 'general'};
        assert.equal(h.context.chatTimeline.hold(msg, {}), false);
        h.context.appendMessage(msg); h.context.chatTimeline.appended(msg, {});
        h.context.fetch = async () => ({status: 404, ok: false});
        await h.context.chatTimeline.latest();
        assert.equal(h.context.chatTimeline.detached(), false);
    }
    assert.deepEqual(h.ids(), Array.from({length: 502}, (_, i) => i));
    h.context.chatTimeline.readLatest();
    assert.equal(h.ids().at(-1), 501); assert.equal(h.ids().length, 500);
});

test('reply data precedes mounted DOM and escapes sender and excerpt', () => {
    const h = harness();
    h.context.chatTimeline.remember({id: 1, sender: '<img>', text: '<script>fixture</script>'});
    const html = h.context.chatTimeline.quote(1);
    assert.match(html, /&lt;img&gt;/); assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<img>|<script>/); assert.match(html, /scrollToMessage\(1\)/);
});

test('forward page stays detached when a live arrival is newer than its snapshot', async () => {
    const h = harness(); await h.detach(); h.context.autoScroll = false;
    h.context.chatTimeline.hold({id: 500, channel: 'general'}, {});
    h.timeline.scrollTop = 24000;
    let finish; h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    h.context.document.getElementById('timeline-newer').onclick();
    h.context.chatTimeline.hold({id: 501, channel: 'general'}, {});
    finish(h.response({messages: h.rows(500, 500), has_before: true, has_after: false}));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.context.chatTimeline.detached(), true);
    assert.equal(h.ids().at(-1), 500);
});

test('channel activation retains unread while detached and latest clears it', async () => {
    const h = harness(); await h.detach(); h.context.autoScroll = false;
    h.context.chatTimeline.hold({id: 500, channel: 'general'}, {});
    h.context.chatTimeline.arrival({id: 500, channel: 'general'});
    h.context.chatTimeline.save('general'); h.context.activeChannel = 'work';
    h.context.chatTimeline.arrival({id: 501, channel: 'general'});
    h.context.activeChannel = 'general'; h.context.chatTimeline.activate('general');
    assert.equal(h.context.unreadCount, 2);
    h.context.fetch = async () => h.response({messages: h.rows(2, 501), has_before: true, has_after: false});
    await h.context.chatTimeline.latest();
    assert.equal(h.context.unreadCount, 0);
});

test('a reader who moves into the trim edge while waiting is never evicted', async () => {
    const h = harness(); h.seed(500, 999);
    let finish; h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    h.context.document.getElementById('timeline-earlier').onclick();
    h.timeline.scrollTop = 24000;
    finish(h.response({messages: h.rows(400, 499), has_before: true, has_after: true}));
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(h.ids(), Array.from({length: 500}, (_, i) => i + 500));
});

test('preview cache stays bounded across a long browsing session', () => {
    const h = harness();
    for (let id = 0; id < 1500; id++) h.context.chatTimeline.remember({id, sender: 'Max', text: 'Fixture'});
    assert.match(h.context.chatTimeline.quote(0), /View reply/);
    assert.match(h.context.chatTimeline.quote(1499), /reply-sender/);
});

test('reply sender/excerpt exactly match the accepted original live quote template', () => {
    const h = harness();
    const block = fs.readFileSync(path.join(__dirname, 'fixtures/reply-quote-886e01d.js'), 'utf8');
    for (const text of ['same consistent preview', 'x'.repeat(80), 'x'.repeat(81), '<b>escaped & quoted</b>']) {
        const parent = {dataset: {rawText: text}, querySelector: () => ({textContent: 'ad-cbt-uat', style: {color: 'blue'}})};
        const original = vm.runInNewContext(block + '\nreplyHtml', {msg: {reply_to: 1}, document: {querySelector: () => parent}, escapeHtml: h.context.escapeHtml});
        h.context.chatTimeline.remember({id: 1, sender: 'ad-cbt-uat', text});
        const inside = html => html.replace(/^<[^>]*>/, '').replace(/<\/[^>]+>$/, '');
        assert.equal(inside(h.context.chatTimeline.quote(1)), inside(original));
    }
});

test('page controls remain disabled until scroll restoration has settled', async () => {
    const h = harness(); h.seed(100, 599); const frames = [];
    h.context.requestAnimationFrame = fn => frames.push(fn);
    h.context.fetch = async () => h.response({messages: h.rows(0, 99), has_before: false, has_after: true});
    h.context.document.getElementById('timeline-earlier').onclick();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.context.chatTimeline.moving, true);
    assert.equal(h.timeline.style.overflowAnchor, 'none');
    assert.equal(h.context.document.getElementById('timeline-earlier').disabled, true);
    frames.forEach(fn => fn());
    assert.equal(h.context.chatTimeline.moving, false);
    assert.equal(h.timeline.style.overflowAnchor, 'auto');
    assert.equal(h.context.document.getElementById('timeline-earlier').disabled, false);
});

test('reaching the oldest boundary hides its control before restoring the anchor', async () => {
    const h = harness(); h.seed(100, 599);
    const offset = h.context.document.getElementById('message-100').getBoundingClientRect().top;
    h.context.fetch = async () => h.response({messages: h.rows(0, 99), has_before: false, has_after: true});
    h.context.document.getElementById('timeline-earlier').onclick();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.context.document.getElementById('timeline-earlier').hidden, true);
    assert.equal(h.context.document.getElementById('message-100').getBoundingClientRect().top, offset);
});

test('the actual renderer disables entry motion for history but preserves live motion', () => {
    const h = harness();
    const source = fs.readFileSync(path.join(__dirname, '../static/chat.js'), 'utf8');
    const start = source.indexOf('function appendMessage(');
    const end = source.indexOf('\nfunction getSenderClass(', start);
    assert.ok(start >= 0 && end > start);
    Object.assign(h.context, {chatTimeline: undefined, maybeInsertDateDivider() {},
        getMessageDate: () => 'Today', trackMessageArrival() {}, scrollToBottom() {}});
    vm.runInContext(source.slice(start, end), h.context);
    h.context.appendMessage({id: 1, sender: 'system', type: 'system', text: 'Saved', channel: 'general'}, {history: true});
    h.context.appendMessage({id: 2, sender: 'system', type: 'system', text: 'Live', channel: 'general'});
    assert.equal(h.context.document.getElementById('message-1').style.animation, 'none');
    assert.equal(h.context.document.getElementById('message-2').style.animation, undefined);
});

test('the final forward page trims to the cap when it reaches latest', async () => {
    const h = harness(); await h.detach(); h.context.autoScroll = false; h.timeline.scrollTop = 24000;
    h.context.fetch = async () => h.response({messages: h.rows(500, 599), has_before: true, has_after: false});
    h.context.document.getElementById('timeline-newer').onclick();
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(h.context.chatTimeline.detached(), false);
    assert.deepEqual(h.ids(), Array.from({length: 500}, (_, i) => i + 100));
});

test('loaded reply quotes retain the accepted original element shape', () => {
    const h = harness(); h.context.chatTimeline.remember({id: 1, sender: 'Max', text: 'Fixture'});
    assert.match(h.context.chatTimeline.quote(1), /^<div class="reply-quote"/);
    assert.match(h.context.chatTimeline.quote(999), /^<button/);
});

test('deleted-parent markers obey the same bounded preview cache', () => {
    const h = harness();
    h.context.chatTimeline.event({type: 'delete', ids: Array.from({length: 1500}, (_, i) => i)});
    assert.match(h.context.chatTimeline.quote(0), /View reply/);
    assert.match(h.context.chatTimeline.quote(1499), /unavailable/);
});

test('returning to the oldest channel settles controls before restoring its anchor', async () => {
    const h = harness(); await h.detach(); h.timeline.scrollTop = 0;
    const offset = h.context.document.getElementById('message-0').getBoundingClientRect().top;
    h.context.chatTimeline.save('general'); h.context.activeChannel = 'work'; h.context.chatTimeline.activate('work');
    h.context.activeChannel = 'general'; h.context.chatTimeline.activate('general');
    assert.equal(h.context.document.getElementById('message-0').getBoundingClientRect().top, offset);
});

test('the real live renderer and grouping helper create one ordered job group', () => {
    const h = harness();
    const jobs = fs.readFileSync(path.join(__dirname, '../static/jobs.js'), 'utf8');
    vm.runInContext(jobs.slice(jobs.indexOf('function _collapseJobBreadcrumbs('), jobs.indexOf('function _repairJobGroup(')), h.context);
    const source = fs.readFileSync(path.join(__dirname, '../static/chat.js'), 'utf8');
    const start = source.indexOf('function appendMessage(');
    Object.assign(h.context, {maybeInsertDateDivider() {}, getMessageDate: () => 'Today',
        trackMessageArrival() {}, scrollToBottom() {},
        _messageRenderers: {job_created: el => el.classList.add('job-breadcrumb')}});
    vm.runInContext(source.slice(start, source.indexOf('\nfunction getSenderClass(', start)), h.context);
    for (let id = 1; id <= 3; id++) h.context.appendMessage({id, sender: 'system', type: 'job_created', text: 'Fixture', channel: 'general'});
    assert.equal(h.box.querySelectorAll('.job-group').length, 1);
    assert.deepEqual(h.ids(), [1, 2, 3]);
});

test('deleting a channel invalidates its pending page and bounded client state', async () => {
    const h = harness(); h.context.activeChannel = 'work'; h.context.channelList = ['general', 'work'];
    const msg = {id: 1, sender: 'Max', text: 'Fixture', channel: 'work'};
    h.context.chatTimeline.remember(msg); h.context.chatTimeline.hold(msg, {history: true});
    h.context.appendMessage(msg); h.context.chatTimeline.settle([msg]);
    let finish; h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = h.context.chatTimeline.latest();
    h.context.chatTimeline.event({type: 'clear', channel: 'work'});
    h.box.querySelectorAll('.message[data-id]').filter(el => el.dataset.channel === 'work').forEach(el => el.remove());
    h.context.channelList = ['general']; h.context.activeChannel = 'general';
    finish(h.response({messages: [msg], has_before: false, has_after: false})); await pending;
    assert.equal(h.box.querySelectorAll('.message[data-id]').filter(el => el.dataset.channel === 'work').length, 0);
    assert.match(h.context.chatTimeline.quote(1), /unavailable|View reply/);
});

test('the actual rename event migrates grouped message channels as well as wrappers', () => {
    const h = harness(); h.context.activeChannel = 'work'; h.context.channelList = ['general', 'work'];
    const group = h.context.document.createElement('div'); group.className = 'job-group'; group.dataset.channel = 'work';
    for (let id = 1; id <= 2; id++) {
        const el = h.context.document.createElement('div'); el.className = 'message job-breadcrumb';
        el.dataset = {id: String(id), channel: 'work'}; group.append(el);
    }
    h.box.append(group);
    Object.assign(h.context, {Hub: {emit() {}}, _channelMentions: {}, pendingChannelSwitch: null,
        localStorage: {setItem() {}}, Store: {set() {}}, filterMessagesByChannel() {}});
    const source = fs.readFileSync(path.join(__dirname, '../static/chat.js'), 'utf8');
    const start = source.indexOf('function handleServerEvent(');
    vm.runInContext(source.slice(start, source.indexOf('\nfunction connectWebSocket(', start)), h.context);
    h.context.handleServerEvent({type: 'channel_renamed', old_name: 'work', new_name: 'renamed'});
    assert.equal(h.context.activeChannel, 'renamed');
    assert.ok(h.box.querySelectorAll('[data-channel]').every(el => el.dataset.channel === 'renamed'));
});
