/* =============================================================
   chat.js  —  Nature's Academy conversational UI
   Requires: jQuery 3.7+
   Save to: static/chat/chat.js
   ============================================================= */

$(function () {

  /* ── CSRF helper ───────────────────────────────────────────── */
  function csrf() {
    return $('[name=csrfmiddlewaretoken]').val();
  }

  $.ajaxSetup({ headers: { 'X-CSRFToken': csrf() } });

  /* ── State ─────────────────────────────────────────────────── */
  let currentConvId      = window.CHAT_STATE.activeConvId || null;
  let pendingFolderId    = null;   // folder to assign when new conv is created
  let isStreaming        = false;
  const IS_AUTH          = window.CHAT_STATE.isAuthenticated;

  /* ── DOM refs ──────────────────────────────────────────────── */
  const $msgContainer  = $('#messages');
  const $chatForm      = $('#chat-form');
  const $msgInput      = $chatForm.find('textarea[name="message"]');
  const $sendBtn       = $('#send-btn');
  const $convList      = $('#conv-list');
  const $sidebarBody   = $('#sidebar-body');
  const $statusEl      = $('#status-indicator');
  const $statusText    = $('#status-text');
  const $sidebar       = $('#sidebar');
  const $overlay       = $('#sidebar-overlay');

  /* ─────────────────────────────────────────────────────────────
     MODAL ENGINE
  ───────────────────────────────────────────────────────────── */

  function openModal(id) {
    $('#' + id).addClass('is-open');
    // Focus first input
    setTimeout(() => $('#' + id).find('.na-modal__input').first().trigger('focus'), 60);
  }

  function closeModal(id) {
    const $m = $('#' + id);
    $m.removeClass('is-open');
    $m.find('.na-modal__input').val('').removeClass('na-modal__input--error');
    $m.find('.na-modal__error').text('');
  }

  // Close via X or Cancel buttons
  $(document).on('click', '[data-dismiss]', function () {
    closeModal($(this).data('dismiss'));
  });

  // Close on backdrop click
  $(document).on('click', '.na-modal__backdrop', function () {
    $(this).closest('.na-modal').removeClass('is-open');
  });

  // Close on Escape
  $(document).on('keydown', function (e) {
    if (e.key === 'Escape') $('.na-modal.is-open').removeClass('is-open');
  });

  /* ─────────────────────────────────────────────────────────────
     HELPERS
  ───────────────────────────────────────────────────────────── */

  function escHtml(s) {
    return $('<div>').text(s).html();
  }

  function scrollToBottom() {
    const bubbles = $msgContainer.find('.bubble');
    if (bubbles.length) bubbles.last()[0].scrollIntoView({ behavior: 'smooth', block: 'end' });
  }

  function showStatus(msg) { $statusText.text(msg); $statusEl.show(); }
  function hideStatus()    { $statusEl.hide(); }

  function setLoading(on) {
    isStreaming = on;
    $sendBtn.prop('disabled', on);
    $msgInput.prop('disabled', on);
    $('#send-icon').toggle(!on);
    $('#stop-icon').toggle(on);
  }

  function removeEmptyState() { $('#empty-state').remove(); }

  // Matches (in priority order, single pass so we never re-scan text we've
  // already wrapped in a tag): URLs, emails, then phone-like number groups.
  // Phone matching is heuristic — requires 8-12 total digits across at
  // least two grouped chunks, so plain numbers in prose ("600 hours",
  // "2026") don't get wrongly linked.
  const LINKIFY_RE = new RegExp(
    '(https?://[^\\s<]+[^\\s<.,:;"\')\\]])' +
    '|([\\w.+-]+@[\\w-]+\\.[\\w.-]+)' +
    '|(\\+?\\d{1,3}?[-.\\s]?\\(?\\d{2,4}\\)?[-.\\s]?\\d{3,4}[-.\\s]?\\d{3,4}(?:[-.\\s]?\\d{2,4})?)',
    'g'
  );

  function linkify(escapedText) {
    return escapedText.replace(LINKIFY_RE, function (match, url, email, phone) {
      if (url) {
        return `<a href="${url}" target="_blank" rel="noopener">${url}</a>`;
      }
      if (email) {
        return `<a href="mailto:${email}">${email}</a>`;
      }
      if (phone) {
        const digitCount = (phone.match(/\d/g) || []).length;
        if (digitCount < 8 || digitCount > 12) return match; // not phone-like enough
        const dial = phone.replace(/[^\d+]/g, '');
        return `<a href="tel:${dial}">${phone}</a>`;
      }
      return match;
    });
  }

  // Full pipeline for assistant message text: escape HTML, auto-link
  // URLs/emails/phone numbers, then convert newlines to <br>.
  function formatAssistantContent(text) {
    return linkify(escHtml(text || '')).replace(/\n/g, '<br>');
  }

  function addBubble(role, html, sourcesHtml) {
    const label = role === 'user' ? 'You' : 'Assistant';
    const cls   = role === 'user' ? 'user' : 'bot';
    const $b = $(`<div class="bubble ${cls}">
      <div class="role">${label}</div>
      <div class="content">${html}</div>
      ${sourcesHtml || ''}
    </div>`);
    $msgContainer.append($b);
    scrollToBottom();
    return $b;
  }

  // Build clickable source-card HTML from a `sources` array
  // (each item: {source_name, source_url, chunk_pk, score}).
  // Dedupes by source_url (falling back to source_name) since several
  // retrieved chunks often come from the same document.
  function renderSourceCards(sources) {
    if (!sources || !sources.length) return '';
    const seen  = new Set();
    const cards = [];
    sources.forEach(function (s) {
      const key = s.source_url || s.source_name;
      if (!key || seen.has(key)) return;
      seen.add(key);
      const name = escHtml(s.source_name || 'Source');
      if (s.source_url) {
        const url = escHtml(s.source_url);
        cards.push(
          `<a class="source-card" href="${url}" target="_blank" rel="noopener" title="${url}">` +
          `<i class="fa-solid fa-arrow-up-right-from-square"></i> ${name}</a>`
        );
      } else {
        cards.push(`<span class="source-card source-card--nolink">${name}</span>`);
      }
    });
    return cards.length ? `<div class="source-cards">${cards.join('')}</div>` : '';
  }

  /* ─────────────────────────────────────────────────────────────
     SIDEBAR RENDER HELPERS
  ───────────────────────────────────────────────────────────── */

  function convItemInnerHTML(id, title) {
    const moveBtn = IS_AUTH
      ? `<button class="conv-move" data-id="${id}" title="Move to folder"><i class="fa-solid fa-arrow-right-arrow-left"></i></button>`
      : '';
    return `
      <span class="conv-title" title="${escHtml(title)}">${escHtml(title)}</span>
      <div class="conv-actions">
        ${moveBtn}
        <button class="conv-rename" data-id="${id}" title="Rename"><i class="fa-solid fa-pencil"></i></button>
        <button class="conv-delete" data-id="${id}" title="Delete"><i class="fa-solid fa-trash"></i></button>
      </div>`;
  }

  function setActiveConv(id) {
    $('.conv-item').removeClass('conv-item--active');
    const $el = $(`.conv-item[data-id="${id}"]`);
    if ($el.length) {
      $el.addClass('conv-item--active');
      // Auto-expand parent folder if collapsed
      $el.closest('.folder-group').removeClass('folder-group--collapsed');
    }
  }

  // Add/update a conversation in the unfiled list
  function upsertUnfiledConv(id, title) {
    $('#conv-empty').remove();
    let $item = $convList.find(`[data-id="${id}"]`);
    if (!$item.length) {
      $item = $('<li class="conv-item"></li>').attr({ 'data-id': id, 'data-folder-id': '' });
      $convList.prepend($item);
    }
    $item.html(convItemInnerHTML(id, title));
    setActiveConv(id);
  }

  // Build folder option rows for move modal
  function buildFolderOptions(currentFolderId) {
    const $list = $('#move-folder-options').empty();

    // Unfiled option
    const isUnfiled = !currentFolderId;
    $list.append(
      $('<button class="na-modal__folder-option" type="button"></button>')
        .addClass(isUnfiled ? 'is-current' : '')
        .attr('data-target-folder', '')
        .html(`
          <i class="fa-solid fa-inbox folder-opt-unfiled"></i>
          <span class="folder-opt-name">Unfiled (Recent)</span>
          <i class="fa-solid fa-check folder-opt-check"></i>`)
    );

    // Each folder
    $('.folder-group').each(function () {
      const fid   = $(this).data('folder-id');
      const fname = $(this).find('.folder-name').first().text().trim();
      const isCur = (fid === currentFolderId);
      $list.append(
        $('<button class="na-modal__folder-option" type="button"></button>')
          .addClass(isCur ? 'is-current' : '')
          .attr('data-target-folder', fid)
          .html(`
            <i class="fa-solid fa-folder folder-opt-icon"></i>
            <span class="folder-opt-name">${escHtml(fname)}</span>
            <i class="fa-solid fa-check folder-opt-check"></i>`)
      );
    });
  }

  /* ─────────────────────────────────────────────────────────────
     LOAD CONVERSATION
  ───────────────────────────────────────────────────────────── */

  function loadConversation(id) {
    $.get(`/api/conversations/${id}/`, function (data) {
      currentConvId = id;
      $msgContainer.empty();
      if (!data.messages.length) {
        $msgContainer.html('<div class="empty" id="empty-state"><p>No messages yet.</p></div>');
      } else {
        $.each(data.messages, function (_, m) {
          const html = m.role === 'user'
            ? escHtml(m.content)
            : formatAssistantContent(m.content);
          const sourcesHtml = m.role === 'assistant'
            ? renderSourceCards(m.source_chunks)
            : '';
          addBubble(m.role, html, sourcesHtml);
        });
      }
      setActiveConv(id);
      history.pushState({}, '', `/?conv=${id}`);
    });
  }

  /* ─────────────────────────────────────────────────────────────
     SIDEBAR CLICK DELEGATION
  ───────────────────────────────────────────────────────────── */

  $sidebarBody.on('click', function (e) {

    /* ── conv: load ──────────────────────────── */
    const $convItem = $(e.target).closest('.conv-item');
    if ($convItem.length && !$(e.target).closest('.conv-actions').length) {
      const id = $convItem.data('id');
      if (id !== currentConvId) loadConversation(id);
      if (isMobile()) closeSidebar();
      return;
    }

    /* ── conv: rename ────────────────────────── */
    const $renameConv = $(e.target).closest('.conv-rename');
    if ($renameConv.length) {
      const id    = $renameConv.data('id');
      const title = $(`.conv-item[data-id="${id}"] .conv-title`).first().text();
      $('#rename-conv-name').val(title);
      $('#modal-rename-conv').data('conv-id', id);
      openModal('modal-rename-conv');
      return;
    }

    /* ── conv: delete ────────────────────────── */
    const $deleteConv = $(e.target).closest('.conv-delete');
    if ($deleteConv.length) {
      const id = $deleteConv.data('id');
      $('#modal-delete-conv').data('conv-id', id);
      const title = $(`.conv-item[data-id="${id}"] .conv-title`).first().text();
      $('#delete-conv-name-display').text(title);
      openModal('modal-delete-conv');
      return;
    }

    /* ── conv: move ──────────────────────────── */
    const $moveConv = $(e.target).closest('.conv-move');
    if ($moveConv.length) {
      const id          = $moveConv.data('id');
      const folderId    = $(`.conv-item[data-id="${id}"]`).data('folder-id') || '';
      const title       = $(`.conv-item[data-id="${id}"] .conv-title`).first().text();
      $('#move-conv-name-display').text(title);
      $('#modal-move-conv').data('conv-id', id);
      buildFolderOptions(folderId);
      openModal('modal-move-conv');
      return;
    }

    /* ── folder: toggle ──────────────────────── */
    const $folderToggle = $(e.target).closest('.folder-toggle');
    if ($folderToggle.length) {
      $folderToggle.closest('.folder-group').toggleClass('folder-group--collapsed');
      return;
    }

    /* ── folder: rename ──────────────────────── */
    const $folderRename = $(e.target).closest('.folder-rename-btn');
    if ($folderRename.length) {
      e.stopPropagation();
      const fid  = $folderRename.data('folder-id');
      const name = $(`#folder-${fid} .folder-name`).first().text().trim();
      $('#rename-folder-name').val(name);
      $('#modal-rename-folder').data('folder-id', fid);
      openModal('modal-rename-folder');
      return;
    }

    /* ── folder: delete ──────────────────────── */
    const $folderDelete = $(e.target).closest('.folder-delete-btn');
    if ($folderDelete.length) {
      e.stopPropagation();
      const fid  = $folderDelete.data('folder-id');
      const name = $(`#folder-${fid} .folder-name`).first().text().trim();
      $('#delete-folder-name-display').text(name);
      $('#modal-delete-folder').data('folder-id', fid);
      openModal('modal-delete-folder');
      return;
    }

    /* ── folder: new conversation in folder ──── */
    const $newConvInFolder = $(e.target).closest('.folder-new-conv-btn');
    if ($newConvInFolder.length) {
      e.stopPropagation();
      const fid  = $newConvInFolder.data('folder-id');
      const name = $(`#folder-${fid} .folder-name`).first().text().trim();
      $('#new-conv-folder-name').text(name);
      $('#modal-new-conv-in-folder').data('folder-id', fid);
      openModal('modal-new-conv-in-folder');
      return;
    }
  });

  /* ─────────────────────────────────────────────────────────────
     MODAL CONFIRMS
  ───────────────────────────────────────────────────────────── */

  /* New folder ─────────────────────────────────────────────── */
  $('#new-folder-btn').on('click', function () {
    openModal('modal-new-folder');
  });

  $('#new-folder-confirm').on('click', function () {
    const name = $('#new-folder-name').val().trim();
    if (!name) {
      $('#new-folder-error').text('Please enter a folder name.');
      $('#new-folder-name').addClass('na-modal__input--error');
      return;
    }
    const $btn = $(this).prop('disabled', true);
    $.ajax({
      url: '/api/folders/',
      method: 'POST',
      contentType: 'application/json',
      data: JSON.stringify({ name }),
      success(data) {
        closeModal('modal-new-folder');
        injectFolderGroup(data.id, data.name);
      },
      error(xhr) {
        const msg = xhr.responseJSON?.error || 'Could not create folder.';
        $('#new-folder-error').text(msg);
        $('#new-folder-name').addClass('na-modal__input--error');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* Rename folder ──────────────────────────────────────────── */
  $('#rename-folder-confirm').on('click', function () {
    const fid  = $('#modal-rename-folder').data('folder-id');
    const name = $('#rename-folder-name').val().trim();
    if (!name) {
      $('#rename-folder-error').text('Please enter a name.');
      return;
    }
    const $btn = $(this).prop('disabled', true);
    $.ajax({
      url: `/api/folders/${fid}/`,
      method: 'PATCH',
      contentType: 'application/json',
      data: JSON.stringify({ name }),
      success(data) {
        $(`#folder-${fid} .folder-name`).first().text(data.name);
        closeModal('modal-rename-folder');
      },
      error(xhr) {
        $('#rename-folder-error').text(xhr.responseJSON?.error || 'Could not rename.');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* Delete folder ──────────────────────────────────────────── */
  $('#delete-folder-confirm').on('click', function () {
    const fid  = $('#modal-delete-folder').data('folder-id');
    const $btn = $(this).prop('disabled', true);
    $.ajax({
      url: `/api/folders/${fid}/`,
      method: 'DELETE',
      success() {
        // Move folder's conversations to unfiled
        $(`#folder-convs-${fid} .conv-item`).each(function () {
          const $item = $(this);
          const id    = $item.data('id');
          const title = $item.find('.conv-title').text();
          $item.attr('data-folder-id', '').html(convItemInnerHTML(id, title));
          $('#conv-empty').remove();
          $convList.prepend($item);
        });
        $(`#folder-${fid}`).remove();
        setActiveConv(currentConvId);
        closeModal('modal-delete-folder');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* Rename conversation ────────────────────────────────────── */
  $('#rename-conv-confirm').on('click', function () {
    const id    = $('#modal-rename-conv').data('conv-id');
    const title = $('#rename-conv-name').val().trim();
    if (!title) {
      $('#rename-conv-error').text('Please enter a name.');
      return;
    }
    const $btn = $(this).prop('disabled', true);
    $.ajax({
      url: `/api/conversations/${id}/`,
      method: 'PATCH',
      contentType: 'application/json',
      data: JSON.stringify({ title }),
      success(data) {
        $(`.conv-item[data-id="${id}"] .conv-title`).text(data.title).attr('title', data.title);
        closeModal('modal-rename-conv');
      },
      error(xhr) {
        $('#rename-conv-error').text(xhr.responseJSON?.error || 'Could not rename.');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* Delete conversation ────────────────────────────────────── */
  // (modal_delete_conv is inline in index.html since it's simple)
  $('#delete-conv-confirm').on('click', function () {
    const id   = $('#modal-delete-conv').data('conv-id');
    const $btn = $(this).prop('disabled', true);
    $.ajax({
      url: `/api/conversations/${id}/`,
      method: 'DELETE',
      success() {
        const $item      = $(`.conv-item[data-id="${id}"]`);
        const $parentList = $item.parent();
        $item.remove();
        if (currentConvId === id) startNewChat();
        if (!$parentList.find('.conv-item').length) {
          const isEmpty = $parentList.attr('id') === 'conv-list';
          $parentList.html(isEmpty
            ? '<li class="conv-empty" id="conv-empty">No conversations yet</li>'
            : '<li class="conv-empty-folder">No conversations yet</li>');
        }
        closeModal('modal-delete-conv');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* Move conversation ──────────────────────────────────────── */
  $(document).on('click', '.na-modal__folder-option', function () {
    if ($(this).hasClass('is-current')) return;
    const convId         = $('#modal-move-conv').data('conv-id');
    const targetFolderId = $(this).attr('data-target-folder') || null;
    const $btn           = $(this).prop('disabled', true);

    $.ajax({
      url: `/api/conversations/${convId}/`,
      method: 'PATCH',
      contentType: 'application/json',
      data: JSON.stringify({ folder_id: targetFolderId }),
      success() {
        const $item  = $(`.conv-item[data-id="${convId}"]`);
        const $pList = $item.parent();
        const title  = $item.find('.conv-title').text();

        $item.attr('data-folder-id', targetFolderId || '');
        $item.html(convItemInnerHTML(convId, title));

        if (targetFolderId) {
          const $targetList = $(`#folder-convs-${targetFolderId}`);
          $targetList.find('.conv-empty-folder').remove();
          $targetList.prepend($item);
          $(`#folder-${targetFolderId}`).removeClass('folder-group--collapsed');
        } else {
          $('#conv-empty').remove();
          $convList.prepend($item);
        }

        // Clean up empty parent list
        if (!$pList.find('.conv-item').length) {
          $pList.html($pList.attr('id') === 'conv-list'
            ? '<li class="conv-empty" id="conv-empty">No conversations yet</li>'
            : '<li class="conv-empty-folder">No conversations yet</li>');
        }
        setActiveConv(currentConvId);
        closeModal('modal-move-conv');
      },
      complete() { $btn.prop('disabled', false); }
    });
  });

  /* New conversation in folder ─────────────────────────────── */
  $('#new-conv-in-folder-confirm').on('click', function () {
    const fid = $('#modal-new-conv-in-folder').data('folder-id');
    closeModal('modal-new-conv-in-folder');
    startNewChat(fid);
  });

  /* ─────────────────────────────────────────────────────────────
     NEW FOLDER DOM INJECTION
  ───────────────────────────────────────────────────────────── */

  function injectFolderGroup(fid, name) {
    const html = `
      <div class="folder-group" data-folder-id="${fid}" id="folder-${fid}">
        <div class="folder-header">
          <button class="folder-toggle" data-folder-id="${fid}">
            <i class="fa-solid fa-chevron-right folder-chevron"></i>
          </button>
          <i class="fa-solid fa-folder folder-icon"></i>
          <span class="folder-name">${escHtml(name)}</span>
          <div class="folder-actions">
            <button class="folder-action-btn folder-new-conv-btn" data-folder-id="${fid}" title="New conversation in folder">
              <i class="fa-solid fa-pen-to-square"></i>
            </button>
            <button class="folder-action-btn folder-rename-btn" data-folder-id="${fid}" title="Rename">
              <i class="fa-solid fa-pencil"></i>
            </button>
            <button class="folder-action-btn folder-delete-btn" data-folder-id="${fid}" title="Delete">
              <i class="fa-solid fa-trash"></i>
            </button>
          </div>
        </div>
        <ul class="folder-conv-list" id="folder-convs-${fid}">
          <li class="conv-empty-folder">No conversations yet</li>
        </ul>
      </div>`;

    const $recentLabel = $sidebarBody.find('.sidebar-section-label').first();
    if ($recentLabel.length) {
      $recentLabel.before(html);
    } else {
      $convList.before(html);
    }
  }

  /* ─────────────────────────────────────────────────────────────
     NEW CHAT
  ───────────────────────────────────────────────────────────── */

  function startNewChat(folderId) {
    currentConvId   = null;
    pendingFolderId = folderId || null;
    $msgContainer.html(`
      <div class="empty" id="empty-state">
        <img src="/static/images/logo.png" alt="Nature's Academy" width="100"/>
        <h1>Welcome to Nature's Academy</h1>
        <p>Ask me anything about our courses and programmes.</p>
      </div>`);
    $('.conv-item').removeClass('conv-item--active');
    $msgInput.trigger('focus');
    history.pushState({}, '', '/');
  }

  $('#new-chat-btn').on('click', () => startNewChat());

  /* ─────────────────────────────────────────────────────────────
     STREAMING SEND
  ───────────────────────────────────────────────────────────── */

  async function sendStream(message) {
    const payload = { message, conversation_id: currentConvId };
    if (pendingFolderId) payload.folder_id = pendingFolderId;

    const resp = await fetch(window.CHAT_STATE.streamUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error('Network error');

    const reader     = resp.body.getReader();
    const decoder    = new TextDecoder();
    const $botBubble = addBubble('bot', '');
    const $content   = $botBubble.find('.content');
    let   rawText    = '';

    return reader.read().then(function pump({ done, value }) {
      if (done) { setLoading(false); hideStatus(); return; }
      decoder.decode(value).split('\n').forEach(line => {
        if (!line.startsWith('data: ')) return;
        try {
          const ev = JSON.parse(line.slice(6));
          if      (ev.type === 'status')         showStatus(ev.message);
          else if (ev.type === 'clear_status')   hideStatus();
          else if (ev.type === 'content')      {
            // Live preview during streaming: HTML-escaped only (no
            // linkification yet — a URL/phone number can arrive split
            // across two chunks, and linkifying a half-URL would either
            // miss it or produce a broken link). Full linkify pass runs
            // once on the complete text when the 'done' event arrives.
            rawText += ev.content;
            $content.html(escHtml(rawText).replace(/\n/g, '<br>'));
            scrollToBottom();
          }
          else if (ev.type === 'conversation_meta') {
            currentConvId   = ev.conversation_id;
            pendingFolderId = null;

            if (ev.folder_id) {
              // Place into folder list
              const $fList = $(`#folder-convs-${ev.folder_id}`);
              if ($fList.length) {
                $fList.find('.conv-empty-folder').remove();
                const $item = $('<li class="conv-item conv-item--active"></li>')
                  .attr({ 'data-id': ev.conversation_id, 'data-folder-id': ev.folder_id })
                  .html(convItemInnerHTML(ev.conversation_id, ev.conversation_title));
                $fList.prepend($item);
                $(`#folder-${ev.folder_id}`).removeClass('folder-group--collapsed');
              }
            } else {
              upsertUnfiledConv(ev.conversation_id, ev.conversation_title);
            }
            history.replaceState({}, '', `/?conv=${ev.conversation_id}`);
          }
          else if (ev.type === 'done')  {
            hideStatus();
            setLoading(false);
            $content.html(formatAssistantContent(rawText));
            const sourcesHtml = renderSourceCards(ev.sources);
            if (sourcesHtml) $botBubble.append(sourcesHtml);
          }
          else if (ev.type === 'error') { hideStatus(); $content.html('<em>Error. Please try again.</em>'); setLoading(false); }
        } catch (_) {}
      });
      return reader.read().then(pump);
    });
  }

  async function sendRegular(message) {
    const payload = { message, conversation_id: currentConvId };
    if (pendingFolderId) payload.folder_id = pendingFolderId;

    const data = await $.ajax({
      url: window.CHAT_STATE.sendUrl,
      method: 'POST',
      contentType: 'application/json',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      data: JSON.stringify(payload),
    });

    if (data.success) {
      addBubble('bot', formatAssistantContent(data.assistant_message));
      currentConvId   = data.conversation_id;
      pendingFolderId = null;
      upsertUnfiledConv(data.conversation_id, data.conversation_title);
      history.replaceState({}, '', `/?conv=${data.conversation_id}`);
    } else {
      addBubble('bot', '<em>Sorry, an error occurred.</em>');
    }
    setLoading(false);
  }

  /* ─────────────────────────────────────────────────────────────
     FORM SUBMIT
  ───────────────────────────────────────────────────────────── */

  $chatForm.on('submit', async function (e) {
    e.preventDefault();
    const message = $msgInput.val().trim();
    if (!message || isStreaming) return;
    removeEmptyState();
    addBubble('user', escHtml(message));
    $msgInput.val('');
    setLoading(true);
    try { await sendStream(message); }
    catch (err) { console.warn('Stream failed, falling back', err); await sendRegular(message); }
    $msgInput.trigger('focus');
  });

  $msgInput.on('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $chatForm.trigger('submit'); }
  });

  $sendBtn.on('click', function (e) {
    if (isStreaming) { e.preventDefault(); setLoading(false); hideStatus(); }
  });

  /* ─────────────────────────────────────────────────────────────
     SIDEBAR COLLAPSE / MOBILE
  ───────────────────────────────────────────────────────────── */

  const MOBILE_BP = 768;
  function isMobile() { return $(window).width() <= MOBILE_BP; }

  function openSidebar() {
    if (isMobile()) { $sidebar.addClass('sidebar--mobile-open'); $overlay.addClass('sidebar-overlay--visible'); }
    else            { $sidebar.removeClass('sidebar--collapsed'); }
  }
  function closeSidebar() {
    if (isMobile()) { $sidebar.removeClass('sidebar--mobile-open'); $overlay.removeClass('sidebar-overlay--visible'); }
    else            { $sidebar.addClass('sidebar--collapsed'); }
  }

  $('#sidebar-toggle').on('click', function () {
    if (isMobile()) {
      $sidebar.hasClass('sidebar--mobile-open') ? closeSidebar() : openSidebar();
    } else {
      $sidebar.hasClass('sidebar--collapsed') ? openSidebar() : closeSidebar();
    }
  });

  $overlay.on('click', closeSidebar);

  $(window).on('resize', function () {
    if (!isMobile()) { $sidebar.removeClass('sidebar--mobile-open'); $overlay.removeClass('sidebar-overlay--visible'); }
  });

  /* ─────────────────────────────────────────────────────────────
     ENTER KEY IN MODALS
  ───────────────────────────────────────────────────────────── */

  $('#new-folder-name').on('keydown',    e => { if (e.key==='Enter') $('#new-folder-confirm').trigger('click'); });
  $('#rename-folder-name').on('keydown', e => { if (e.key==='Enter') $('#rename-folder-confirm').trigger('click'); });
  $('#rename-conv-name').on('keydown',   e => { if (e.key==='Enter') $('#rename-conv-confirm').trigger('click'); });

  /* ── Init ──────────────────────────────────────────────────── */
  scrollToBottom();
  $msgInput.trigger('focus');
});