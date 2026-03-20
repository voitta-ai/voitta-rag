/**
 * UI utilities: toast notifications, spinner, escapeHtml, keyboard shortcuts.
 */
import { state } from './state.js';

export function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    container.appendChild(toast);

    const duration = type === 'error' ? 8000 : 4000;
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

function clearSpinnerTimeout() {
    if (state.spinnerTimeout) {
        clearTimeout(state.spinnerTimeout);
        state.spinnerTimeout = null;
    }
}

export function showSpinner(delay = 0) {
    clearSpinnerTimeout();
    if (delay > 0) {
        state.spinnerTimeout = setTimeout(() => {
            const overlay = document.getElementById('spinner-overlay');
            if (overlay) overlay.classList.add('active');
        }, delay);
    } else {
        const overlay = document.getElementById('spinner-overlay');
        if (overlay) overlay.classList.add('active');
    }
}

export function hideSpinner() {
    clearSpinnerTimeout();
    const overlay = document.getElementById('spinner-overlay');
    if (overlay) overlay.classList.remove('active');
}

export function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Escape to close modals
document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        const modal = document.querySelector('.modal.active');
        if (modal) {
            modal.classList.remove('active');
        }
    }
});
