/* Shared behaviour for the staff console.
 *
 * No framework and no build step: the clinic deploys by pulling the repo, so
 * everything here is plain ES2020 that runs from a single <script> tag. Each
 * feature degrades to a working page if its API is unavailable.
 */
(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // Toast notifications
    // -----------------------------------------------------------------------
    var toastHost = null;

    function ensureToastHost() {
        if (toastHost) return toastHost;
        toastHost = document.createElement('div');
        toastHost.className = 'toast-host';
        toastHost.setAttribute('role', 'status');
        toastHost.setAttribute('aria-live', 'polite');
        document.body.appendChild(toastHost);
        return toastHost;
    }

    function toast(message, kind, timeout) {
        if (!message) return;
        var host = ensureToastHost();
        var node = document.createElement('div');
        node.className = 'toast toast-' + (kind || 'success');
        node.innerHTML =
            '<span class="toast-text"></span>' +
            '<button type="button" class="toast-close" aria-label="ปิดข้อความ">&times;</button>';
        node.querySelector('.toast-text').textContent = message;
        node.querySelector('.toast-close').addEventListener('click', function () {
            dismissToast(node);
        });
        host.appendChild(node);
        // Force a reflow so the entry transition actually runs.
        void node.offsetWidth;
        node.classList.add('is-visible');
        window.setTimeout(function () { dismissToast(node); }, timeout || 6000);
    }

    function dismissToast(node) {
        if (!node || node.dataset.dismissed) return;
        node.dataset.dismissed = '1';
        node.classList.remove('is-visible');
        window.setTimeout(function () { node.remove(); }, 250);
    }

    window.warfarinToast = toast;

    /* A redirect carries its message in the query string. Show it as a toast
     * and strip it from the URL so a refresh does not repeat it. */
    function consumeFlashParams() {
        var params = new URLSearchParams(window.location.search);
        var message = params.get('msg');
        if (!message) return;
        toast(message, params.get('kind') || 'success');
        params.delete('msg');
        params.delete('kind');
        var query = params.toString();
        window.history.replaceState(
            {}, '',
            window.location.pathname + (query ? '?' + query : '') + window.location.hash
        );
        var inline = document.getElementById('flashMessage');
        if (inline) inline.remove();
    }

    // -----------------------------------------------------------------------
    // Command palette — patient search without leaving the keyboard
    // -----------------------------------------------------------------------
    var PALETTE_ACTIONS = [
        { label: 'แดชบอร์ด', hint: 'ภาพรวมวันนี้', url: '/dashboard' },
        { label: 'รายชื่อผู้ป่วย', hint: 'ค้นหาและแก้ไข', url: '/patients' },
        { label: 'เพิ่มผู้ป่วยใหม่', hint: 'ลงทะเบียนผู้ป่วย', url: '/patients/new' },
        { label: 'ผู้ป่วยที่ต้องติดตาม', hint: 'กลุ่มเสี่ยง', url: '/dashboard/at-risk' },
        { label: 'ยังไม่ยืนยันวันนี้', hint: 'รายการติดตามวันนี้', url: '/dashboard/missed-today' },
        { label: 'นัดหมาย / INR', hint: 'ตารางนัด', url: '/appointments' },
        { label: 'อาการผู้ป่วย', hint: 'รอตอบกลับ', url: '/symptoms' },
        { label: 'สแกน QR', hint: 'ยืนยันยาด้วยกล้อง', url: '/scan' },
        { label: 'รายงานผลลัพธ์', hint: 'ตัวชี้วัดคลินิก', url: '/reports' },
        { label: 'งานวิจัย', hint: 'ผู้เข้าร่วมและผลวิเคราะห์', url: '/research' },
        { label: 'วิเคราะห์ผลงานวิจัย', hint: 'ตารางสถิติ', url: '/research/analysis' },
        { label: 'คู่มือผู้ป่วย', hint: 'ความรู้เรื่องยา', url: '/education' },
    ];

    var palette = {
        root: null, input: null, list: null, items: [], index: 0, timer: null,
    };

    function buildPalette() {
        var root = document.createElement('div');
        root.className = 'palette';
        root.hidden = true;
        root.innerHTML =
            '<div class="palette-backdrop" data-close="1"></div>' +
            '<div class="palette-panel" role="dialog" aria-modal="true" aria-label="ค้นหาและไปยังหน้า">' +
            '  <input class="palette-input" type="search" autocomplete="off" spellcheck="false"' +
            '         placeholder="พิมพ์ชื่อผู้ป่วย HN หรือชื่อหน้า…" aria-label="ค้นหา">' +
            '  <ul class="palette-list" role="listbox"></ul>' +
            '  <div class="palette-foot">↑ ↓ เลื่อน · Enter เปิด · Esc ปิด</div>' +
            '</div>';
        document.body.appendChild(root);
        palette.root = root;
        palette.input = root.querySelector('.palette-input');
        palette.list = root.querySelector('.palette-list');

        root.addEventListener('click', function (event) {
            if (event.target.dataset.close) closePalette();
        });
        palette.input.addEventListener('input', function () {
            window.clearTimeout(palette.timer);
            palette.timer = window.setTimeout(runPaletteSearch, 140);
        });
        palette.input.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown') { event.preventDefault(); moveSelection(1); }
            else if (event.key === 'ArrowUp') { event.preventDefault(); moveSelection(-1); }
            else if (event.key === 'Enter') {
                event.preventDefault();
                var chosen = palette.items[palette.index];
                if (chosen) window.location.href = chosen.url;
            }
        });
    }

    function openPalette() {
        if (!palette.root) buildPalette();
        palette.root.hidden = false;
        palette.input.value = '';
        palette.input.focus();
        renderPalette(PALETTE_ACTIONS.slice(0, 8));
        document.body.style.overflow = 'hidden';
    }

    function closePalette() {
        if (!palette.root) return;
        palette.root.hidden = true;
        document.body.style.overflow = '';
    }

    function moveSelection(step) {
        if (!palette.items.length) return;
        palette.index = (palette.index + step + palette.items.length) % palette.items.length;
        highlightSelection();
    }

    function highlightSelection() {
        Array.prototype.forEach.call(palette.list.children, function (node, position) {
            node.classList.toggle('is-active', position === palette.index);
            if (position === palette.index) node.scrollIntoView({ block: 'nearest' });
        });
    }

    function renderPalette(items) {
        palette.items = items;
        palette.index = 0;
        palette.list.innerHTML = '';
        if (!items.length) {
            var empty = document.createElement('li');
            empty.className = 'palette-empty';
            empty.textContent = 'ไม่พบผลลัพธ์';
            palette.list.appendChild(empty);
            return;
        }
        items.forEach(function (item, position) {
            var node = document.createElement('li');
            node.className = 'palette-item' + (position === 0 ? ' is-active' : '');
            node.setAttribute('role', 'option');
            var label = document.createElement('span');
            label.className = 'palette-label';
            label.textContent = item.label;
            var hint = document.createElement('span');
            hint.className = 'palette-hint';
            hint.textContent = item.hint || '';
            node.appendChild(label);
            node.appendChild(hint);
            node.addEventListener('click', function () { window.location.href = item.url; });
            palette.list.appendChild(node);
        });
    }

    function runPaletteSearch() {
        var query = palette.input.value.trim();
        var matches = PALETTE_ACTIONS.filter(function (action) {
            return !query || action.label.indexOf(query) !== -1;
        });
        if (query.length < 2) {
            renderPalette(matches.slice(0, 8));
            return;
        }
        fetch('/api/lookup?q=' + encodeURIComponent(query), { credentials: 'same-origin' })
            .then(function (response) { return response.ok ? response.json() : []; })
            .then(function (patients) {
                var results = patients.map(function (patient) {
                    return {
                        label: patient.full_name,
                        hint: 'HN ' + (patient.hn || '-') + (patient.active ? '' : ' · ปิดบัญชี'),
                        url: '/patients/' + patient.patient_id,
                    };
                });
                renderPalette(results.concat(matches).slice(0, 12));
            })
            .catch(function () { renderPalette(matches.slice(0, 8)); });
    }

    // -----------------------------------------------------------------------
    // Form guards: stop the double submit that creates duplicate records
    // -----------------------------------------------------------------------
    function guardForms() {
        document.querySelectorAll('form[method="POST"], form[method="post"]').forEach(function (form) {
            form.addEventListener('submit', function () {
                // `onsubmit` confirm() dialogs run first; if one cancelled the
                // submit this handler never fires, so disabling here is safe.
                var button = form.querySelector('button[type="submit"], button:not([type])');
                if (!button || button.dataset.noGuard) return;
                window.setTimeout(function () {
                    button.disabled = true;
                    button.dataset.originalText = button.textContent;
                    button.textContent = 'กำลังบันทึก…';
                }, 0);
                // Re-enable if the browser restores the page from bfcache.
                window.addEventListener('pageshow', function () {
                    button.disabled = false;
                    if (button.dataset.originalText) button.textContent = button.dataset.originalText;
                });
            });
        });
    }

    // -----------------------------------------------------------------------
    // Keyboard shortcuts
    // -----------------------------------------------------------------------
    function isTypingTarget(element) {
        if (!element) return false;
        var tag = element.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || element.isContentEditable;
    }

    function bindShortcuts() {
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                closePalette();
                return;
            }
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                openPalette();
                return;
            }
            if (isTypingTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
            if (event.key === '/') { event.preventDefault(); openPalette(); }
            else if (event.key === '?') { event.preventDefault(); toggleShortcutHelp(); }
        });

        document.querySelectorAll('[data-open-palette]').forEach(function (trigger) {
            trigger.addEventListener('click', function (event) {
                event.preventDefault();
                openPalette();
            });
        });
    }

    var SHORTCUTS = [
        ['⌘K หรือ Ctrl+K', 'เปิดช่องค้นหา'],
        ['/', 'เปิดช่องค้นหา'],
        ['?', 'แสดงรายการปุ่มลัดนี้'],
        ['Esc', 'ปิดหน้าต่างที่เปิดอยู่'],
    ];

    function toggleShortcutHelp() {
        var existing = document.getElementById('shortcutHelp');
        if (existing) { existing.remove(); return; }
        var box = document.createElement('div');
        box.id = 'shortcutHelp';
        box.className = 'shortcut-help';
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-label', 'ปุ่มลัด');
        var rows = SHORTCUTS.map(function (pair) {
            return '<div class="shortcut-row"><kbd>' + pair[0] + '</kbd><span>' + pair[1] + '</span></div>';
        }).join('');
        box.innerHTML = '<div class="shortcut-title">ปุ่มลัด</div>' + rows +
            '<button type="button" class="btn btn-secondary btn-sm" style="margin-top:10px;">ปิด</button>';
        box.querySelector('button').addEventListener('click', function () { box.remove(); });
        document.body.appendChild(box);
    }

    // -----------------------------------------------------------------------
    // Sticky table headers on long lists
    // -----------------------------------------------------------------------
    function markScrollableTables() {
        document.querySelectorAll('.overflow-x-auto').forEach(function (wrapper) {
            var update = function () {
                wrapper.classList.toggle(
                    'has-overflow', wrapper.scrollWidth > wrapper.clientWidth + 4
                );
            };
            update();
            window.addEventListener('resize', update);
        });
    }

    // -----------------------------------------------------------------------
    document.addEventListener('DOMContentLoaded', function () {
        consumeFlashParams();
        bindShortcuts();
        guardForms();
        markScrollableTables();
    });
})();
