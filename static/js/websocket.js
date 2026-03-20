/**
 * WebSocket connection with exponential backoff and visibility-aware reconnect.
 */
import { state } from './state.js';
import {
    handleFileSystemEvent, handleSyncStatusEvent,
    handleIndexStatusEvent, handleIndexCompleteEvent,
    handleSpConnectedEvent, handleAdoConnectedEvent,
    handleBoxConnectedEvent, handleGdConnectedEvent,
} from './ws-handlers.js';

const MAX_RECONNECT_DELAY = 30000;

export function initWebSocket() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
        console.log('WebSocket connected');
        // Reset backoff on successful connection
        state.wsReconnectDelay = 1000;
        if (state.wsReconnectTimeout) {
            clearTimeout(state.wsReconnectTimeout);
            state.wsReconnectTimeout = null;
        }
    };

    state.ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'ping') return;

        switch (data.type) {
            case 'sync_status':    handleSyncStatusEvent(data); break;
            case 'index_status':   handleIndexStatusEvent(data); break;
            case 'index_complete': handleIndexCompleteEvent(data); break;
            case 'sp_connected':   handleSpConnectedEvent(data); break;
            case 'ado_connected':  handleAdoConnectedEvent(data); break;
            case 'box_connected':  handleBoxConnectedEvent(data); break;
            case 'gd_connected':   handleGdConnectedEvent(data); break;
            default:               handleFileSystemEvent(data); break;
        }
    };

    state.ws.onclose = () => {
        console.log(`WebSocket disconnected, reconnecting in ${state.wsReconnectDelay / 1000}s...`);
        state.wsReconnectTimeout = setTimeout(initWebSocket, state.wsReconnectDelay);
        // Exponential backoff
        state.wsReconnectDelay = Math.min(state.wsReconnectDelay * 2, MAX_RECONNECT_DELAY);
    };

    state.ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

// Reconnect immediately when tab becomes visible (if disconnected)
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
            if (state.wsReconnectTimeout) {
                clearTimeout(state.wsReconnectTimeout);
                state.wsReconnectTimeout = null;
            }
            state.wsReconnectDelay = 1000;
            initWebSocket();
        }
    }
});
