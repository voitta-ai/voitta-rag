/**
 * Remote sync: all sync source management, OAuth connect flows, multi-select widget.
 */
import { state } from './state.js';
import { showToast, escapeHtml } from './ui.js';

// ---- Multi-select checkbox dropdown widget ----

const _msState = {};

export function msInit(id, items, selectedValues) {
    _msState[id] = { items: items, selected: new Set(selectedValues || []) };
    _msRender(id);
    document.getElementById(id + '-dropdown')?.classList.remove('open');
}

function _msRender(id) {
    const dropdown = document.getElementById(id + '-dropdown');
    const textEl = document.getElementById(id + '-text');
    if (!dropdown || !textEl) return;

    const s = _msState[id];
    if (!s) return;

    dropdown.innerHTML = '';

    if (s.items.length === 0) {
        const hint = document.createElement('div');
        hint.className = 'ms-empty';
        hint.textContent = 'Click "Fetch" to load options';
        dropdown.appendChild(hint);
    } else {
        // ALL option
        const allDiv = document.createElement('div');
        allDiv.className = 'ms-option ms-all';
        const allCb = document.createElement('input');
        allCb.type = 'checkbox';
        allCb.checked = s.selected.has('*');
        allCb.onchange = () => _msToggleAll(id, allCb.checked);
        const allLabel = document.createElement('span');
        allLabel.textContent = '-- ALL --';
        allDiv.appendChild(allCb);
        allDiv.appendChild(allLabel);
        dropdown.appendChild(allDiv);

        const allSelected = s.selected.has('*');
        s.items.forEach(item => {
            const div = document.createElement('div');
            div.className = 'ms-option' + (allSelected ? ' ms-disabled' : '');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = s.selected.has(item.value) || allSelected;
            cb.disabled = allSelected;
            cb.onchange = () => _msToggleItem(id, item.value, cb.checked);
            const label = document.createElement('span');
            label.textContent = item.label;
            div.appendChild(cb);
            div.appendChild(label);
            dropdown.appendChild(div);
        });
    }

    _msUpdateText(id);
}

function _msToggleAll(id, checked) {
    const s = _msState[id];
    if (!s) return;
    if (checked) {
        s.selected.clear();
        s.selected.add('*');
    } else {
        s.selected.clear();
    }
    _msRender(id);
}

function _msToggleItem(id, value, checked) {
    const s = _msState[id];
    if (!s) return;
    s.selected.delete('*');
    if (checked) {
        s.selected.add(value);
    } else {
        s.selected.delete(value);
    }
    if (s.items.length > 0 && s.selected.size === s.items.length) {
        s.selected.clear();
        s.selected.add('*');
    }
    _msRender(id);
}

function _msUpdateText(id) {
    const textEl = document.getElementById(id + '-text');
    const s = _msState[id];
    if (!textEl || !s) return;

    if (s.selected.has('*')) {
        textEl.textContent = 'All (syncs all current & future)';
        textEl.classList.add('has-value');
    } else if (s.selected.size === 0) {
        textEl.textContent = s.items.length === 0 ? 'Click Fetch to load' : 'None selected';
        textEl.classList.remove('has-value');
    } else if (s.selected.size <= 3) {
        textEl.textContent = Array.from(s.selected).join(', ');
        textEl.classList.add('has-value');
    } else {
        textEl.textContent = s.selected.size + ' selected';
        textEl.classList.add('has-value');
    }
}

export function msToggle(id) {
    const dropdown = document.getElementById(id + '-dropdown');
    if (!dropdown) return;
    const isOpen = dropdown.classList.contains('open');
    document.querySelectorAll('.ms-dropdown.open').forEach(d => d.classList.remove('open'));
    if (!isOpen) {
        dropdown.classList.add('open');
    }
}

export function msGetValue(id) {
    const s = _msState[id];
    if (!s) return '';
    if (s.selected.has('*')) return '*';
    return Array.from(s.selected).join(',');
}

export function msSetValue(id, csvValue) {
    if (!_msState[id]) {
        _msState[id] = { items: [], selected: new Set() };
    }
    const s = _msState[id];
    s.selected.clear();
    if (!csvValue) return;
    if (csvValue === '*') {
        s.selected.add('*');
    } else {
        csvValue.split(',').forEach(k => {
            const trimmed = k.trim();
            if (trimmed) s.selected.add(trimmed);
        });
    }
    _msRender(id);
}

export function msReset(id) {
    _msState[id] = { items: [], selected: new Set() };
    const dropdown = document.getElementById(id + '-dropdown');
    if (dropdown) {
        dropdown.innerHTML = '';
        dropdown.classList.remove('open');
    }
    const textEl = document.getElementById(id + '-text');
    if (textEl) {
        textEl.textContent = 'Click Fetch to load';
        textEl.classList.remove('has-value');
    }
    msSetDisabled(id, false);
}

export function msSetDisabled(id, disabled) {
    const container = document.getElementById(id + '-container');
    if (container) {
        container.classList.toggle('disabled', disabled);
    }
}

// Close dropdowns when clicking outside
document.addEventListener('click', (e) => {
    if (!e.target.closest('.ms-container')) {
        document.querySelectorAll('.ms-dropdown.open').forEach(d => d.classList.remove('open'));
    }
});

// ---- Sync source management ----

export function onSyncSourceTypeChange(value) {
    document.querySelectorAll('.sync-fields').forEach(el => el.style.display = 'none');

    if (value) {
        const fields = document.getElementById(`sync-fields-${value}`);
        if (fields) fields.style.display = 'block';
        document.getElementById('sync-actions').style.display = 'flex';
    } else {
        document.getElementById('sync-actions').style.display =
            state.currentSyncSource ? 'flex' : 'none';
    }
}

export function clearSyncFields() {
    state.currentSyncSource = null;
    const select = document.getElementById('sync-source-type');
    if (select) {
        select.value = '';
        select.disabled = false;
    }
    document.querySelectorAll('.sync-fields').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.sync-input, .sync-textarea').forEach(el => {
        el.value = '';
        el.disabled = false;
    });
    document.querySelectorAll('.sync-fields .sync-select').forEach(el => {
        el.disabled = false;
    });
    msReset('jira-project');
    msReset('confluence-space');
    const actions = document.getElementById('sync-actions');
    if (actions) actions.style.display = 'none';
    const saveBtn = document.getElementById('btn-sync-save');
    const removeBtn = document.getElementById('btn-sync-remove');
    if (saveBtn) saveBtn.style.display = '';
    if (removeBtn) removeBtn.style.display = '';
    const connectBtn = document.getElementById('btn-sp-connect');
    if (connectBtn) connectBtn.style.display = '';
    const statusDisplay = document.getElementById('sync-status-display');
    if (statusDisplay) statusDisplay.style.display = 'none';
}

export function updateSyncStatusDisplay(data) {
    const display = document.getElementById('sync-status-display');
    const statusValue = document.getElementById('sync-status-value');
    const lastSynced = document.getElementById('sync-last-synced');

    if (!data || !data.source_type) {
        if (display) display.style.display = 'none';
        return;
    }

    if (display) display.style.display = 'flex';

    const statusLabels = {
        'idle': 'Idle',
        'syncing': 'Syncing...',
        'synced': 'Synced',
        'error': 'Error',
    };
    if (statusValue) {
        let label = statusLabels[data.sync_status] || data.sync_status;
        if (data.sync_status === 'error' && data.sync_error) {
            label = 'Error: ' + data.sync_error;
        }
        statusValue.textContent = label;
        statusValue.className = `sync-status-value sync-status-${data.sync_status}`;
        statusValue.title = data.sync_error || '';
    }

    if (lastSynced) {
        if (data.last_synced_at) {
            const d = new Date(data.last_synced_at);
            lastSynced.textContent = `Last: ${d.toLocaleString()}`;
        } else {
            lastSynced.textContent = 'Never synced';
        }
    }

    const syncBtn = document.getElementById('btn-sync-trigger');
    if (syncBtn) {
        syncBtn.disabled = data.sync_status === 'syncing';
    }
}

export function updateFolderSyncStatus(syncStatus, lastSyncedAt) {
    const row = document.getElementById('folder-sync-status-row');
    const value = document.getElementById('folder-sync-status-value');
    const lastSynced = document.getElementById('folder-sync-last-synced');
    if (!row) return;

    if (!syncStatus) {
        row.style.display = 'none';
        return;
    }

    row.style.display = 'flex';

    const labels = { 'idle': 'Idle', 'syncing': 'Syncing...', 'synced': 'Synced', 'error': 'Error' };
    if (value) {
        value.textContent = labels[syncStatus] || syncStatus;
        value.className = `sync-status-value sync-status-${syncStatus}`;
    }
    if (lastSynced) {
        if (lastSyncedAt) {
            const d = new Date(lastSyncedAt);
            lastSynced.textContent = `Last: ${d.toLocaleString()}`;
        } else {
            lastSynced.textContent = '';
        }
    }
}

export function populateSyncFields(data) {
    state.currentSyncSource = data;

    if (data.sync_status === 'syncing' && data.folder_path) {
        state.syncingFolders.add(data.folder_path);
    }

    const locked = data.source_type && !state.currentFolderIsEmpty;

    const select = document.getElementById('sync-source-type');
    select.value = data.source_type || '';
    select.disabled = locked;
    onSyncSourceTypeChange(data.source_type || '');

    if (data.source_type === 'sharepoint' && data.sharepoint) {
        document.getElementById('sp-tenant-id').value = data.sharepoint.tenant_id || '';
        document.getElementById('sp-client-id').value = data.sharepoint.client_id || '';
        document.getElementById('sp-client-secret').value = data.sharepoint.client_secret || '';
        document.getElementById('sp-site-url').value = data.sharepoint.site_url || '';
        document.getElementById('sp-drive-id').value = data.sharepoint.drive_id || '';
        updateSpConnectStatus(data.sharepoint.connected);
        const allSitesCb = document.getElementById('sp-all-sites');
        if (allSitesCb) allSitesCb.checked = !!data.sharepoint.all_sites;
        const sitesSection = document.getElementById('sp-sites-section');
        if (sitesSection) {
            if (data.sharepoint.connected) {
                sitesSection.style.display = 'block';
                fetchSharePointSites(data.sharepoint.selected_sites || '');
            } else {
                sitesSection.style.display = 'none';
            }
        }
    } else if (data.source_type === 'google_drive' && data.google_drive) {
        document.getElementById('gd-client-id').value = data.google_drive.client_id || '';
        document.getElementById('gd-client-secret').value = data.google_drive.client_secret || '';
        updateGdConnectStatus(data.google_drive.connected);
        if (data.google_drive.connected) {
            fetchGoogleDriveFolders(data.google_drive.folder_id || '');
        } else {
            const folderSelect = document.getElementById('gd-folder-id');
            folderSelect.innerHTML = '<option value="">Select a folder...</option>';
            if (data.google_drive.folder_id) {
                const opt = document.createElement('option');
                opt.value = data.google_drive.folder_id;
                opt.textContent = data.google_drive.folder_id;
                folderSelect.appendChild(opt);
                folderSelect.value = data.google_drive.folder_id;
            }
        }
    } else if (data.source_type === 'github' && data.github) {
        document.getElementById('gh-repo').value = data.github.repo || '';
        document.getElementById('gh-path').value = data.github.path || '';
        document.getElementById('gh-auth-method').value = data.github.auth_method || 'ssh';
        document.getElementById('gh-ssh-key').value = data.github.ssh_key || '';
        document.getElementById('gh-username').value = data.github.username || '';
        document.getElementById('gh-pat').value = data.github.token || '';
        document.getElementById('gh-all-branches').checked = !!data.github.all_branches;
        toggleGhAuth();
        toggleAllBranches();
        const savedBranch = data.github.branch || 'main';
        if (data.github.repo) {
            fetchGitBranches(savedBranch);
        } else {
            document.getElementById('gh-branch').innerHTML = '<option value="main">main</option>';
        }
    } else if (data.source_type === 'azure_devops' && data.azure_devops) {
        document.getElementById('ado-tenant-id').value = data.azure_devops.tenant_id || '';
        document.getElementById('ado-client-id').value = data.azure_devops.client_id || '';
        document.getElementById('ado-client-secret').value = data.azure_devops.client_secret || '';
        document.getElementById('ado-url').value = data.azure_devops.url || '';
        updateAdoConnectStatus(data.azure_devops.connected);
    } else if (data.source_type === 'jira' && data.jira) {
        document.getElementById('jira-url').value = data.jira.url || '';
        document.getElementById('jira-auth-method').value = data.jira.auth_method || 'cloud';
        document.getElementById('jira-email').value = data.jira.email || '';
        if ((data.jira.auth_method || 'cloud') === 'cloud') {
            document.getElementById('jira-token').value = data.jira.token || '';
        } else {
            document.getElementById('jira-token-server').value = data.jira.token || '';
        }
        toggleJiraAuth();
        const savedProject = data.jira.project || '';
        const projectKeys = savedProject === '*' ? ['*'] : savedProject.split(',').map(k => k.trim()).filter(Boolean);
        if (savedProject && data.jira.url && data.jira.token) {
            fetchJiraProjects(projectKeys);
        } else {
            msInit('jira-project', [], projectKeys);
        }
    } else if (data.source_type === 'confluence' && data.confluence) {
        document.getElementById('confluence-url').value = data.confluence.url || '';
        document.getElementById('confluence-auth-method').value = data.confluence.auth_method || 'cloud';
        document.getElementById('confluence-email').value = data.confluence.email || '';
        if ((data.confluence.auth_method || 'cloud') === 'cloud') {
            document.getElementById('confluence-token').value = data.confluence.token || '';
        } else {
            document.getElementById('confluence-token-server').value = data.confluence.token || '';
        }
        toggleConfluenceAuth();
        const savedSpace = data.confluence.space || '';
        const spaceKeys = savedSpace === '*' ? ['*'] : savedSpace.split(',').map(k => k.trim()).filter(Boolean);
        if (savedSpace && data.confluence.url && data.confluence.token) {
            fetchConfluenceSpaces(spaceKeys);
        } else {
            msInit('confluence-space', [], spaceKeys);
        }
    } else if (data.source_type === 'box' && data.box) {
        document.getElementById('box-client-id').value = data.box.client_id || '';
        document.getElementById('box-client-secret').value = data.box.client_secret || '';
        document.getElementById('box-folder-id').value = data.box.folder_id || '';
        updateBoxConnectStatus(data.box.connected);
    } else if (data.source_type === 'glue_catalog' && data.glue_catalog) {
        document.getElementById('glue-region').value = data.glue_catalog.region || '';
        document.getElementById('glue-auth-method').value = data.glue_catalog.auth_method || 'profile';
        document.getElementById('glue-profile').value = data.glue_catalog.profile || '';
        document.getElementById('glue-access-key-id').value = data.glue_catalog.access_key_id || '';
        document.getElementById('glue-secret-access-key').value = data.glue_catalog.secret_access_key || '';
        document.getElementById('glue-catalog-id').value = data.glue_catalog.catalog_id || '';
        document.getElementById('glue-databases').value = data.glue_catalog.databases || '';
        toggleGlueAuth();
    }

    if (!locked) {
        document.querySelectorAll('.sync-fields .sync-select').forEach(el => el.disabled = false);
        document.querySelectorAll('.sync-input, .sync-textarea').forEach(el => el.disabled = false);
        msSetDisabled('jira-project', false);
        msSetDisabled('confluence-space', false);
    }

    if (locked) {
        document.querySelectorAll('.sync-input, .sync-textarea, .sync-fields .sync-select').forEach(el => el.disabled = true);
        msSetDisabled('jira-project', true);
        msSetDisabled('confluence-space', true);
        const saveBtn = document.getElementById('btn-sync-save');
        const removeBtn = document.getElementById('btn-sync-remove');
        if (saveBtn) saveBtn.style.display = 'none';
        if (removeBtn) removeBtn.style.display = 'none';
    }

    updateSyncStatusDisplay(data);
}

export function gatherSyncConfig() {
    const sourceType = document.getElementById('sync-source-type').value;
    if (!sourceType) return null;

    const config = { source_type: sourceType };

    if (sourceType === 'sharepoint') {
        config.sharepoint = {
            tenant_id: document.getElementById('sp-tenant-id').value.trim(),
            client_id: document.getElementById('sp-client-id').value.trim(),
            client_secret: document.getElementById('sp-client-secret').value.trim(),
            site_url: document.getElementById('sp-site-url').value.trim(),
            drive_id: document.getElementById('sp-drive-id').value.trim(),
            all_sites: document.getElementById('sp-all-sites')?.checked || false,
            selected_sites: _getSelectedSitesJson(),
        };
    } else if (sourceType === 'google_drive') {
        config.google_drive = {
            client_id: document.getElementById('gd-client-id').value.trim(),
            client_secret: document.getElementById('gd-client-secret').value.trim(),
            folder_id: document.getElementById('gd-folder-id').value.trim(),
        };
    } else if (sourceType === 'github') {
        config.github = {
            repo: document.getElementById('gh-repo').value.trim(),
            branch: document.getElementById('gh-branch').value.trim() || 'main',
            path: document.getElementById('gh-path').value.trim(),
            auth_method: document.getElementById('gh-auth-method').value,
            ssh_key: document.getElementById('gh-ssh-key').value.trim(),
            username: document.getElementById('gh-username').value.trim(),
            token: document.getElementById('gh-pat').value.trim(),
            all_branches: document.getElementById('gh-all-branches').checked,
        };
    } else if (sourceType === 'azure_devops') {
        config.azure_devops = {
            tenant_id: document.getElementById('ado-tenant-id').value.trim(),
            client_id: document.getElementById('ado-client-id').value.trim(),
            client_secret: document.getElementById('ado-client-secret').value.trim(),
            url: document.getElementById('ado-url').value.trim(),
        };
    } else if (sourceType === 'jira') {
        const jiraMethod = document.getElementById('jira-auth-method').value;
        const jiraToken = jiraMethod === 'cloud'
            ? document.getElementById('jira-token').value.trim()
            : document.getElementById('jira-token-server').value.trim();
        config.jira = {
            url: document.getElementById('jira-url').value.trim(),
            project: msGetValue('jira-project'),
            auth_method: jiraMethod,
            email: document.getElementById('jira-email').value.trim(),
            token: jiraToken,
        };
    } else if (sourceType === 'confluence') {
        const confMethod = document.getElementById('confluence-auth-method').value;
        const confToken = confMethod === 'cloud'
            ? document.getElementById('confluence-token').value.trim()
            : document.getElementById('confluence-token-server').value.trim();
        config.confluence = {
            url: document.getElementById('confluence-url').value.trim(),
            space: msGetValue('confluence-space'),
            auth_method: confMethod,
            email: document.getElementById('confluence-email').value.trim(),
            token: confToken,
        };
    } else if (sourceType === 'box') {
        config.box = {
            client_id: document.getElementById('box-client-id').value.trim(),
            client_secret: document.getElementById('box-client-secret').value.trim(),
            folder_id: document.getElementById('box-folder-id').value.trim(),
        };
    } else if (sourceType === 'glue_catalog') {
        const glueMethod = document.getElementById('glue-auth-method').value;
        config.glue_catalog = {
            region: document.getElementById('glue-region').value,
            auth_method: glueMethod,
            profile: document.getElementById('glue-profile').value.trim(),
            access_key_id: document.getElementById('glue-access-key-id').value.trim(),
            secret_access_key: document.getElementById('glue-secret-access-key').value.trim(),
            catalog_id: document.getElementById('glue-catalog-id').value.trim(),
            databases: document.getElementById('glue-databases').value.trim(),
        };
    }

    return config;
}

export async function saveSyncSource() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    const config = gatherSyncConfig();
    if (!config) {
        showToast('Select a sync source type', 'error');
        return;
    }

    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save sync source');
        }

        const data = await response.json();
        state.currentSyncSource = data;
        updateSyncStatusDisplay(data);
        showToast('Sync source saved', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function removeSyncSource() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) return;

    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to remove sync source');
        }

        clearSyncFields();
        updateFolderSyncStatus(null, null);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function triggerRemoteSync() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) {
        showToast('No folder selected', 'error');
        return;
    }

    try {
        await saveSyncSource();

        const response = await fetch(`/api/sync/${encodeURIComponent(targetPath)}/trigger`, {
            method: 'POST',
        });

        const result = await response.json();

        if (!response.ok) {
            throw new Error(result.detail || 'Failed to trigger sync');
        }

        state.syncingFolders.add(targetPath);
        updateSyncStatusDisplay({ ...state.currentSyncSource, sync_status: 'syncing' });
        updateFolderSyncStatus('syncing', null);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function loadSyncSource(folderPath) {
    try {
        const response = await fetch(`/api/sync/${encodeURIComponent(folderPath)}`);
        if (response.ok) {
            const data = await response.json();
            if (data && data.source_type) {
                populateSyncFields(data);
                return;
            }
        }
    } catch (error) {
        // No sync source configured
    }
    clearSyncFields();
}

// ---- Auth toggle helpers ----

export function toggleGhAuth() {
    const method = document.getElementById('gh-auth-method').value;
    document.getElementById('gh-ssh-fields').style.display = method === 'ssh' ? '' : 'none';
    document.getElementById('gh-token-fields').style.display = method === 'token' ? '' : 'none';
}

export function toggleJiraAuth() {
    const method = document.getElementById('jira-auth-method').value;
    document.getElementById('jira-cloud-fields').style.display = method === 'cloud' ? '' : 'none';
    document.getElementById('jira-server-fields').style.display = method === 'server' ? '' : 'none';
}

export function toggleConfluenceAuth() {
    const method = document.getElementById('confluence-auth-method').value;
    document.getElementById('confluence-cloud-fields').style.display = method === 'cloud' ? '' : 'none';
    document.getElementById('confluence-server-fields').style.display = method === 'server' ? '' : 'none';
}

export function toggleGlueAuth() {
    const method = document.getElementById('glue-auth-method').value;
    document.getElementById('glue-profile-fields').style.display = method === 'profile' ? '' : 'none';
    document.getElementById('glue-keys-fields').style.display = method === 'keys' ? '' : 'none';
}

export function showGlueHelp() {
    showToast(
        'Indexes schema metadata (databases, tables, columns) from AWS Glue Data Catalog. ' +
        'Use "Profile" for local dev (~/.aws/credentials) or "Access Keys" for Docker deployments. ' +
        'Leave Databases empty or use * for all databases, or list specific ones separated by commas.',
        'info',
    );
}

export function toggleAllBranches() {
    const checked = document.getElementById('gh-all-branches').checked;
    document.getElementById('gh-branch').disabled = checked;
}

// ---- Connect status helpers ----

export function updateSpConnectStatus(connected) {
    const el = document.getElementById('sp-connect-status');
    const btn = document.getElementById('btn-sp-connect');
    if (!el) return;
    el.className = connected ? 'sp-connect-status connected' : 'sp-connect-status not-connected';
    el.textContent = connected ? 'Connected' : 'Not connected';
    if (btn) btn.textContent = connected ? 'Reconnect' : 'Connect';
}

export function updateAdoConnectStatus(connected) {
    const el = document.getElementById('ado-connect-status');
    const btn = document.getElementById('btn-ado-connect');
    if (!el) return;
    el.className = connected ? 'sp-connect-status connected' : 'sp-connect-status not-connected';
    el.textContent = connected ? 'Connected' : 'Not connected';
    if (btn) btn.textContent = connected ? 'Reconnect' : 'Connect';
}

export function updateBoxConnectStatus(connected) {
    const el = document.getElementById('box-connect-status');
    const btn = document.getElementById('btn-box-connect');
    if (!el) return;
    el.className = connected ? 'sp-connect-status connected' : 'sp-connect-status not-connected';
    el.textContent = connected ? 'Connected' : 'Not connected';
    if (btn) btn.textContent = connected ? 'Reconnect' : 'Connect';
}

export function updateGdConnectStatus(connected) {
    const el = document.getElementById('gd-connect-status');
    const btn = document.getElementById('btn-gd-connect');
    if (!el) return;
    el.className = connected ? 'sp-connect-status connected' : 'sp-connect-status not-connected';
    el.textContent = connected ? 'Connected' : 'Not connected';
    if (btn) btn.textContent = connected ? 'Reconnect' : 'Connect';
}

// ---- Fetch remote data ----

export async function fetchGitBranches(preselectBranch) {
    const repoUrl = document.getElementById('gh-repo').value.trim();
    const authMethod = document.getElementById('gh-auth-method').value;
    const sshKey = document.getElementById('gh-ssh-key').value.trim();
    const ghUsername = document.getElementById('gh-username').value.trim();
    const ghPat = document.getElementById('gh-pat').value.trim();
    const branchSelect = document.getElementById('gh-branch');
    if (!repoUrl) return;

    const credKey = authMethod + '|' + sshKey + '|' + ghUsername + '|' + ghPat;

    if (repoUrl === state.lastBranchUrl && credKey === state.lastBranchCred) {
        if (preselectBranch) branchSelect.value = preselectBranch;
        return;
    }

    const savedValue = preselectBranch || branchSelect.value;
    branchSelect.innerHTML = '<option value="" disabled selected>Loading branches...</option>';
    branchSelect.disabled = true;

    try {
        const params = new URLSearchParams({ repo_url: repoUrl });
        if (authMethod === 'token') {
            if (ghPat) params.set('token', ghPat);
            if (ghUsername) params.set('username', ghUsername);
        } else {
            if (sshKey) params.set('ssh_key', sshKey);
        }
        const resp = await fetch(`/api/sync/git/branches?${params}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const branches = data.branches || [];

        branchSelect.innerHTML = '';
        if (branches.length === 0) {
            branchSelect.innerHTML = '<option value="main">main</option>';
        } else {
            branches.forEach(b => {
                const opt = document.createElement('option');
                opt.value = b;
                opt.textContent = b;
                branchSelect.appendChild(opt);
            });
        }

        if (savedValue && branches.includes(savedValue)) {
            branchSelect.value = savedValue;
        }

        state.lastBranchUrl = repoUrl;
        state.lastBranchCred = credKey;
    } catch (e) {
        branchSelect.innerHTML = '<option value="main">main</option>';
        console.warn('Failed to fetch branches:', e.message);
    } finally {
        branchSelect.disabled = document.getElementById('gh-all-branches').checked;
    }
}

export async function fetchGoogleDriveFolders(preselectFolder) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) return;

    const folderSelect = document.getElementById('gd-folder-id');
    if (!folderSelect) return;

    const savedValue = preselectFolder || folderSelect.value;
    folderSelect.innerHTML = '<option value="" disabled selected>Loading folders...</option>';
    folderSelect.disabled = true;

    try {
        const resp = await fetch(`/api/sync/google-drive/folders?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const myFolders = data.folders || [];
        const sharedFolders = data.shared_folders || [];
        const sharedDrives = data.shared_drives || [];

        folderSelect.innerHTML = '<option value="">Select a folder...</option>';

        if (myFolders.length) {
            const myGroup = document.createElement('optgroup');
            myGroup.label = 'My Drive';
            myFolders.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                myGroup.appendChild(opt);
            });
            folderSelect.appendChild(myGroup);
        }

        if (sharedDrives.length) {
            const drivesGroup = document.createElement('optgroup');
            drivesGroup.label = 'Shared Drives';
            sharedDrives.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                drivesGroup.appendChild(opt);
            });
            folderSelect.appendChild(drivesGroup);
        }

        if (sharedFolders.length) {
            const sharedGroup = document.createElement('optgroup');
            sharedGroup.label = 'Shared with me';
            sharedFolders.forEach(f => {
                const opt = document.createElement('option');
                opt.value = f.id;
                opt.textContent = f.name;
                sharedGroup.appendChild(opt);
            });
            folderSelect.appendChild(sharedGroup);
        }

        if (savedValue) {
            folderSelect.value = savedValue;
        }
    } catch (e) {
        folderSelect.innerHTML = '<option value="">Select a folder...</option>';
        console.warn('Failed to fetch Google Drive folders:', e.message);
    } finally {
        folderSelect.disabled = false;
    }
}

export async function fetchSharePointSites(preselectSites) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) return;

    const listEl = document.getElementById('sp-sites-list');
    if (!listEl) return;

    listEl.innerHTML = '<span style="font-size:12px; color:var(--color-text-tertiary);">Loading sites...</span>';

    try {
        const resp = await fetch(`/api/sync/sharepoint/sites?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const sites = data.sites || [];

        let preselected = [];
        if (preselectSites) {
            try {
                preselected = typeof preselectSites === 'string' ? JSON.parse(preselectSites) : preselectSites;
            } catch (e) { preselected = []; }
        }
        const preselectedIds = new Set(preselected.map(s => s.id));

        listEl.innerHTML = '';
        if (sites.length === 0) {
            listEl.innerHTML = '<span style="font-size:12px; color:var(--color-text-tertiary);">No sites found</span>';
            return;
        }

        const allSitesChecked = document.getElementById('sp-all-sites')?.checked;

        for (const site of sites) {
            const label = document.createElement('label');
            label.className = 'sp-site-item';

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.dataset.siteJson = JSON.stringify(site);
            cb.checked = preselectedIds.has(site.id);
            cb.disabled = allSitesChecked;

            const text = document.createElement('span');
            text.className = 'sp-site-name';
            text.textContent = site.displayName || site.name;
            text.title = site.webUrl || '';

            label.appendChild(cb);
            label.appendChild(text);
            listEl.appendChild(label);
        }
    } catch (e) {
        listEl.innerHTML = `<span style="font-size:12px; color:var(--color-error);">${escapeHtml(e.message)}</span>`;
        console.warn('Failed to fetch SharePoint sites:', e.message);
    }
}

export function toggleAllSites() {
    const checked = document.getElementById('sp-all-sites').checked;
    document.querySelectorAll('#sp-sites-list input[type="checkbox"]').forEach(cb => {
        cb.disabled = checked;
    });
}

function _getSelectedSitesJson() {
    const sites = [];
    document.querySelectorAll('#sp-sites-list input[type="checkbox"]:checked').forEach(cb => {
        try {
            sites.push(JSON.parse(cb.dataset.siteJson));
        } catch (e) {}
    });
    return JSON.stringify(sites);
}

export async function fetchJiraProjects(preselectValues) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) return;

    const savedValues = preselectValues || Array.from(_msState['jira-project']?.selected || []);
    const textEl = document.getElementById('jira-project-text');
    if (textEl) textEl.textContent = 'Loading...';
    msSetDisabled('jira-project', true);

    try {
        const resp = await fetch(`/api/sync/jira/projects?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const projects = data.projects || [];

        const items = projects.map(p => ({ value: p.key, label: `${p.key} - ${p.name}` }));
        msInit('jira-project', items, savedValues);
        showToast(`Found ${projects.length} project(s)`, 'success');
    } catch (e) {
        msInit('jira-project', [], savedValues);
        showToast('Failed to fetch projects: ' + e.message, 'error');
        console.warn('Failed to fetch Jira projects:', e.message);
    } finally {
        msSetDisabled('jira-project', false);
    }
}

export async function fetchConfluenceSpaces(preselectValues) {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) return;

    const savedValues = preselectValues || Array.from(_msState['confluence-space']?.selected || []);
    const textEl = document.getElementById('confluence-space-text');
    if (textEl) textEl.textContent = 'Loading...';
    msSetDisabled('confluence-space', true);

    try {
        const resp = await fetch(`/api/sync/confluence/spaces?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || resp.statusText);
        }
        const data = await resp.json();
        const spaces = data.spaces || [];

        const items = spaces.map(s => ({ value: s.key, label: `${s.key} - ${s.name}` }));
        msInit('confluence-space', items, savedValues);
        showToast(`Found ${spaces.length} space(s)`, 'success');
    } catch (e) {
        msInit('confluence-space', [], savedValues);
        showToast('Failed to fetch spaces: ' + e.message, 'error');
        console.warn('Failed to fetch Confluence spaces:', e.message);
    } finally {
        msSetDisabled('confluence-space', false);
    }
}

// ---- OAuth connect flows ----

async function _oauthConnect(targetPath, config, providerLabel) {
    try {
        const saveResp = await fetch(`/api/sync/${encodeURIComponent(targetPath)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
        if (!saveResp.ok) {
            const error = await saveResp.json();
            throw new Error(error.detail || 'Failed to save config');
        }

        const resp = await fetch(`/api/sync/oauth/auth?folder_path=${encodeURIComponent(targetPath)}`);
        if (!resp.ok) {
            const error = await resp.json();
            throw new Error(error.detail || `Failed to start ${providerLabel} auth`);
        }

        const data = await resp.json();
        window.open(data.auth_url, '_blank');
        showToast(`Sign in to ${providerLabel} in the new tab. This page will update when done.`, 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

export async function connectSharePoint() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) { showToast('No folder selected', 'error'); return; }
    const config = gatherSyncConfig();
    if (!config) { showToast('Select SharePoint and fill in the fields first', 'error'); return; }
    await _oauthConnect(targetPath, config, 'Microsoft');
}

export async function connectAzureDevOps() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) { showToast('No folder selected', 'error'); return; }
    const config = gatherSyncConfig();
    if (!config) { showToast('Select Azure DevOps and fill in the fields first', 'error'); return; }
    await _oauthConnect(targetPath, config, 'Microsoft');
}

export async function connectBox() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) { showToast('No folder selected', 'error'); return; }
    const config = gatherSyncConfig();
    if (!config) { showToast('Select Box and fill in the fields first', 'error'); return; }
    await _oauthConnect(targetPath, config, 'Box');
}

export async function connectGoogleDrive() {
    const targetPath = state.selectedPath || state.currentPath;
    if (!targetPath) { showToast('No folder selected', 'error'); return; }
    const config = gatherSyncConfig();
    if (!config) { showToast('Select Google Drive and fill in the fields first', 'error'); return; }
    await _oauthConnect(targetPath, config, 'Google');
}

export function showGdCredentialsHelp() {
    const modal = document.getElementById('gd-credentials-help-modal');
    const uriEl = document.getElementById('gd-help-redirect-uri');
    uriEl.textContent = window.location.origin + '/api/sync/oauth/callback';
    modal.classList.add('active');
}

export function closeGdCredentialsHelp() {
    document.getElementById('gd-credentials-help-modal').classList.remove('active');
}
