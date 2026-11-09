// Accepted live quote block, verbatim from static/chat.js at 886e01d.
let replyHtml = '';
if (msg.reply_to !== undefined && msg.reply_to !== null) {
    const parentEl = document.querySelector(`.message[data-id="${msg.reply_to}"]`);
    if (parentEl) {
        const parentSender = parentEl.querySelector('.msg-sender')?.textContent || '?';
        const parentText = parentEl.dataset.rawText || parentEl.querySelector('.msg-text')?.textContent || '';
        const truncated = parentText.length > 80 ? parentText.slice(0, 80) + '...' : parentText;
        const parentColor = parentEl.querySelector('.msg-sender')?.style.color || 'var(--text-dim)';
        replyHtml = `<div class="reply-quote" onclick="scrollToMessage(${msg.reply_to})"><span class="reply-sender" style="color: ${parentColor}">${escapeHtml(parentSender)}</span> ${escapeHtml(truncated)}</div>`;
    } else {
        replyHtml = `<button class="reply-quote history-parent" type="button" onclick="chatHistory.context(${Number(msg.reply_to)})">View reply to #${Number(msg.reply_to)}</button>`;
    }
}
