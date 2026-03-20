/**
 * voitta-rag - App orchestrator.
 * Imports all modules and exposes functions to window for onclick handlers in templates.
 */
import { state } from './state.js';
import { toggleTheme } from './theme.js';
import { showToast, showSpinner, hideSpinner, escapeHtml } from './ui.js';
import { refreshFileList, uploadFiles, sortFileList } from './file-ops.js';
import { initWebSocket } from './websocket.js';
import {
    selectItem, loadItemDetails, updateSidebar,
    saveMetadata, toggleFolderEnabled, toggleSearchActive,
    toggleSearchActiveInline, reindexFolder, triggerIndex,
} from './sidebar.js';
import {
    openCreateFolderModal, closeCreateFolderModal, createFolder,
    openDeleteFolderModal, closeDeleteFolderModal, confirmDeleteFolder,
} from './folder-ops.js';
import {
    onProjectChange, openProjectModal, closeProjectModal,
    createProject, deleteProject,
} from './projects.js';
import {
    onSyncSourceTypeChange, saveSyncSource, removeSyncSource, triggerRemoteSync,
    toggleGhAuth, toggleJiraAuth, toggleConfluenceAuth, toggleGlueAuth,
    showGlueHelp, toggleAllBranches, toggleAllSites,
    fetchGitBranches, fetchGoogleDriveFolders, fetchSharePointSites,
    fetchJiraProjects, fetchConfluenceSpaces,
    connectSharePoint, connectAzureDevOps, connectBox, connectGoogleDrive,
    showGdCredentialsHelp, closeGdCredentialsHelp,
    msToggle,
} from './sync.js';

// Expose state and init function for template inline scripts
export { state, initWebSocket, updateSidebar };

// Expose all onclick-referenced functions to window
Object.assign(window, {
    // Theme
    toggleTheme,
    // UI
    showToast, showSpinner, hideSpinner, escapeHtml,
    // File ops
    refreshFileList, uploadFiles, sortFileList,
    // WebSocket
    initWebSocket,
    // Sidebar & settings
    selectItem, loadItemDetails, updateSidebar,
    saveMetadata, toggleFolderEnabled, toggleSearchActive,
    toggleSearchActiveInline, reindexFolder, triggerIndex,
    // Folder ops
    openCreateFolderModal, closeCreateFolderModal, createFolder,
    openDeleteFolderModal, closeDeleteFolderModal, confirmDeleteFolder,
    // Projects
    onProjectChange, openProjectModal, closeProjectModal,
    createProject, deleteProject,
    // Sync
    onSyncSourceTypeChange, saveSyncSource, removeSyncSource, triggerRemoteSync,
    toggleGhAuth, toggleJiraAuth, toggleConfluenceAuth, toggleGlueAuth,
    showGlueHelp, toggleAllBranches, toggleAllSites,
    fetchGitBranches, fetchGoogleDriveFolders, fetchSharePointSites,
    fetchJiraProjects, fetchConfluenceSpaces,
    connectSharePoint, connectAzureDevOps, connectBox, connectGoogleDrive,
    showGdCredentialsHelp, closeGdCredentialsHelp,
    msToggle,
});
