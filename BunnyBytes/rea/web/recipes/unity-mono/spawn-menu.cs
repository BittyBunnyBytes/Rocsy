// {{NAME}}: a Garry's Mod-style spawn menu for any Unity game (Rocsy REA recipe).
// Opens a window listing every prefab the game has loaded; click one to spawn it where you are
// looking. Undo removes the last spawn. Keys are in BepInEx/config/{{GUID}}.cfg.
// Next steps the Master Agent can take after reading the game's code: use the game's own
// spawn/pooling system, group items by category, add a physics gun or a prop freeze tool.
using System;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using BepInEx.Configuration;
using UnityEngine;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BaseUnityPlugin
    {
        ConfigEntry<KeyboardShortcut> menuKey;
        ConfigEntry<KeyboardShortcut> undoKey;
        ConfigEntry<float> maxDistance;
        readonly List<GameObject> prefabs = new List<GameObject>();
        readonly Stack<GameObject> spawned = new Stack<GameObject>();
        Rect window = new Rect(40, 40, 440, 600);
        Vector2 scroll;
        string filter = "";
        bool open, onlyVisible = true;
        CursorLockMode savedLock;
        bool savedVisible;

        void Awake()
        {
            menuKey = Config.Bind("Controls", "Menu", new KeyboardShortcut(KeyCode.Q), "Opens and closes the spawn menu");
            undoKey = Config.Bind("Controls", "Undo", new KeyboardShortcut(KeyCode.Z, KeyCode.LeftControl), "Removes the last spawned object");
            maxDistance = Config.Bind("Spawning", "MaxDistance", 200f, "How far the spawn ray reaches");
            Logger.LogInfo($"{{NAME}} loaded: press {menuKey.Value} for the spawn menu");
        }

        void Update()
        {
            if (menuKey.Value.IsDown()) SetOpen(!open);
            if (undoKey.Value.IsDown()) Undo();
            // Games re-lock the cursor every frame; keep it free while the menu is up.
            if (open) { Cursor.lockState = CursorLockMode.None; Cursor.visible = true; }
        }

        void SetOpen(bool value)
        {
            open = value;
            if (open)
            {
                savedLock = Cursor.lockState; savedVisible = Cursor.visible;
                Refresh();
            }
            else { Cursor.lockState = savedLock; Cursor.visible = savedVisible; }
        }

        // Prefabs = loaded GameObjects that are not part of any scene, top-level only.
        void Refresh()
        {
            prefabs.Clear();
            prefabs.AddRange(Resources.FindObjectsOfTypeAll<GameObject>()
                .Where(g => g != null && g.transform.parent == null && !g.scene.IsValid())
                .Where(g => !onlyVisible || g.GetComponentInChildren<Renderer>(true) != null)
                .GroupBy(g => g.name).Select(g => g.First())
                .OrderBy(g => g.name, StringComparer.OrdinalIgnoreCase));
            Logger.LogInfo($"Spawn menu: {prefabs.Count} prefabs");
        }

        void OnGUI()
        {
            if (!open) return;
            window = GUILayout.Window(0x5EA7, window, DrawWindow, $"Spawn menu ({prefabs.Count})");
        }

        void DrawWindow(int id)
        {
            GUILayout.BeginHorizontal();
            filter = GUILayout.TextField(filter, GUILayout.ExpandWidth(true));
            if (GUILayout.Button("Refresh", GUILayout.Width(70))) Refresh();
            GUILayout.EndHorizontal();
            bool visible = GUILayout.Toggle(onlyVisible, " Only things you can see (have a renderer)");
            if (visible != onlyVisible) { onlyVisible = visible; Refresh(); }
            scroll = GUILayout.BeginScrollView(scroll);
            foreach (var prefab in prefabs)
            {
                if (prefab == null) continue;
                if (filter.Length > 0 && prefab.name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                if (GUILayout.Button(prefab.name)) Spawn(prefab);
            }
            GUILayout.EndScrollView();
            GUILayout.BeginHorizontal();
            if (GUILayout.Button($"Undo ({spawned.Count})")) Undo();
            if (GUILayout.Button("Close")) SetOpen(false);
            GUILayout.EndHorizontal();
            GUI.DragWindow();
        }

        void Spawn(GameObject prefab)
        {
            var cam = Camera.main;
            Vector3 position = Vector3.zero;
            Quaternion rotation = Quaternion.identity;
            if (cam != null)
            {
                var ray = new Ray(cam.transform.position, cam.transform.forward);
                position = Physics.Raycast(ray, out var hit, maxDistance.Value) ? hit.point + hit.normal * 0.5f : ray.GetPoint(5f);
                var flat = Vector3.ProjectOnPlane(-cam.transform.forward, Vector3.up);
                if (flat.sqrMagnitude > 0.001f) rotation = Quaternion.LookRotation(flat);
            }
            try
            {
                var copy = Instantiate(prefab, position, rotation);
                copy.name = prefab.name;
                copy.SetActive(true);
                spawned.Push(copy);
                Logger.LogInfo($"Spawned {prefab.name} at {position}");
            }
            catch (Exception e) { Logger.LogWarning($"Couldn't spawn {prefab.name}: {e.Message}"); }
        }

        void Undo()
        {
            while (spawned.Count > 0)
            {
                var last = spawned.Pop();
                if (last != null) { Destroy(last); return; }
            }
        }
    }
}
