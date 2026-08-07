/* Patient-facing accessibility controls.
 *
 * Warfarin patients are mostly 60+, often reading a LINE link on a phone in
 * bright light. Text size and contrast are therefore first-class controls,
 * not a settings page — and the choice is remembered on the device so an
 * elderly patient sets it once.
 */
(function () {
    'use strict';

    var SCALE_KEY = 'warfarin.textScale';
    var CONTRAST_KEY = 'warfarin.highContrast';
    var SCALES = [1, 1.15, 1.35];
    var SCALE_LABELS = ['ปกติ', 'ใหญ่', 'ใหญ่มาก'];

    function readStorage(key, fallback) {
        try {
            var value = window.localStorage.getItem(key);
            return value === null ? fallback : value;
        } catch (error) {
            return fallback;   // private browsing or storage disabled
        }
    }

    function writeStorage(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (error) {
            /* nothing we can do; the setting just will not persist */
        }
    }

    function applyScale(index) {
        var safe = Math.min(Math.max(parseInt(index, 10) || 0, 0), SCALES.length - 1);
        document.documentElement.style.setProperty('--text-scale', SCALES[safe]);
        document.documentElement.dataset.textScale = String(safe);
        var button = document.getElementById('textScaleButton');
        if (button) {
            button.textContent = 'ขนาดตัวอักษร: ' + SCALE_LABELS[safe];
            button.setAttribute('aria-label', 'ขนาดตัวอักษรปัจจุบัน ' + SCALE_LABELS[safe] + ' — กดเพื่อเปลี่ยน');
        }
        return safe;
    }

    function applyContrast(enabled) {
        document.documentElement.classList.toggle('high-contrast', !!enabled);
        var button = document.getElementById('contrastButton');
        if (button) {
            button.textContent = enabled ? 'โหมดตัดกันสูง: เปิด' : 'โหมดตัดกันสูง: ปิด';
            button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
        }
    }

    function buildControls() {
        var host = document.getElementById('a11yControls');
        if (!host) return;
        host.innerHTML =
            '<button type="button" id="textScaleButton" class="a11y-button"></button>' +
            '<button type="button" id="contrastButton" class="a11y-button" aria-pressed="false"></button>';

        var scaleIndex = applyScale(readStorage(SCALE_KEY, '0'));
        var contrast = readStorage(CONTRAST_KEY, '0') === '1';
        applyContrast(contrast);

        document.getElementById('textScaleButton').addEventListener('click', function () {
            scaleIndex = applyScale((scaleIndex + 1) % SCALES.length);
            writeStorage(SCALE_KEY, String(scaleIndex));
        });
        document.getElementById('contrastButton').addEventListener('click', function () {
            contrast = !contrast;
            applyContrast(contrast);
            writeStorage(CONTRAST_KEY, contrast ? '1' : '0');
        });
    }

    /* Confirming a dose is the one irreversible action a patient takes, and a
     * shaky tap can fire it twice. Disable on submit and say what is happening. */
    function guardConfirmForm() {
        document.querySelectorAll('form[data-confirm-dose]').forEach(function (form) {
            form.addEventListener('submit', function () {
                var button = form.querySelector('button[type="submit"]');
                if (!button) return;
                window.setTimeout(function () {
                    button.disabled = true;
                    button.textContent = 'กำลังบันทึก…';
                }, 0);
            });
        });
    }

    /* Apply the saved preferences before first paint where possible, so the
     * page does not visibly jump for someone using the large setting. */
    (function applyEarly() {
        applyScale(readStorage(SCALE_KEY, '0'));
        applyContrast(readStorage(CONTRAST_KEY, '0') === '1');
    })();

    document.addEventListener('DOMContentLoaded', function () {
        buildControls();
        guardConfirmForm();
    });
})();
