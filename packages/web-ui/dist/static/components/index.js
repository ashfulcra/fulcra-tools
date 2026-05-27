// packages/web-ui/dist/static/components/index.js
//
// Single entry point for the setup_step component bundle. Loaded as a
// module from index.html *after* lit and *before* the wizard.js / page
// scripts that mount the wizard. Order matters because:
//   - Lit must be loaded (the module already imports it).
//   - Components must be registered as custom elements before the first
//     <fulcra-step> appears in the DOM.
//
// Adding a new step kind = three steps:
//   1. extend the Python SetupStep Literal in
//      packages/collect/fulcra_collect/plugin.py
//   2. write packages/web-ui/dist/static/components/step-<kind>.js
//   3. add the import line below
import "./step.js";          // the <fulcra-step> dispatcher
import "./step-intro.js";              // Phase 1
import "./step-external_action.js";    // Phase 2
import "./step-input.js";               // Phase 2
import "./step-oauth.js";               // Phase 2
import "./step-file_upload.js";         // Phase 2
import "./step-permission_request.js";  // Phase 2
import "./step-browser_extension.js";   // Phase 2
import "./step-extension_pair.js";      // Phase 2
import "./step-test_connection.js";     // Phase 2
// import "./step-definition_picker.js";
// import "./step-done.js";
