// {{NAME}}: a Garry's Mod-style spawn menu for a Unity IL2CPP game (Rocsy REA recipe, BepInEx 6).
// Q opens a list of the game's loaded prefabs (IMGUI); click one to spawn it where you look.
// If the game stripped IMGUI (UnityEngine.IMGUIModule missing from BepInEx/interop), the agent
// switches the menu to the game's own UI or to key cycling.
using System;
using System.Collections.Generic;
using System.Linq;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BasePlugin
    {
        internal static ConfigEntry<KeyCode> MenuKey;
        internal static BepInEx.Logging.ManualLogSource Log;

        public override void Load()
        {
            Log = base.Log;
            MenuKey = Config.Bind("Controls", "Menu", KeyCode.Q, "Opens and closes the spawn menu");
            ClassInjector.RegisterTypeInIl2Cpp<SpawnMenu>();
            var host = new GameObject("{{CLASS}}");
            UnityEngine.Object.DontDestroyOnLoad(host);
            host.hideFlags = HideFlags.HideAndDontSave;
            host.AddComponent<SpawnMenu>();
            Log.LogInfo($"{{NAME}} loaded: press {MenuKey.Value} for the spawn menu");
        }
    }

    public class SpawnMenu : MonoBehaviour
    {
        public SpawnMenu(IntPtr ptr) : base(ptr) { }
        readonly List<GameObject> prefabs = new List<GameObject>();
        readonly Stack<GameObject> spawned = new Stack<GameObject>();
        Rect window = new Rect(40, 40, 440, 600);
        Vector2 scroll;
        string filter = "";
        bool open;

        void Update()
        {
            bool pressed;
            try { pressed = Input.GetKeyDown(Plugin.MenuKey.Value); } catch { pressed = false; }
            if (pressed) { open = !open; if (open) Refresh(); }
            if (open) { Cursor.lockState = CursorLockMode.None; Cursor.visible = true; }
        }

        void Refresh()
        {
            prefabs.Clear();
            foreach (var o in Resources.FindObjectsOfTypeAll(Il2CppInterop.Runtime.Il2CppType.Of<GameObject>()))
            {
                var g = o.TryCast<GameObject>();
                if (g != null && g.transform.parent == null && !g.scene.IsValid()) prefabs.Add(g);
            }
            var unique = prefabs.GroupBy(g => g.name).Select(g => g.First()).OrderBy(g => g.name, StringComparer.OrdinalIgnoreCase).ToList();
            prefabs.Clear();
            prefabs.AddRange(unique);
            Plugin.Log.LogInfo($"Spawn menu: {prefabs.Count} prefabs");
        }

        void OnGUI()
        {
            if (!open) return;
            GUILayout.BeginArea(window, GUI.skin.box);
            GUILayout.Label($"Spawn menu ({prefabs.Count})");
            filter = GUILayout.TextField(filter);
            scroll = GUILayout.BeginScrollView(scroll);
            foreach (var p in prefabs)
            {
                if (p == null) continue;
                if (filter.Length > 0 && p.name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                if (GUILayout.Button(p.name)) Spawn(p);
            }
            GUILayout.EndScrollView();
            if (GUILayout.Button($"Undo ({spawned.Count})") && spawned.Count > 0) Destroy(spawned.Pop());
            GUILayout.EndArea();
        }

        void Spawn(GameObject prefab)
        {
            var cam = Camera.main;
            var pos = cam != null ? cam.transform.position + cam.transform.forward * 5f : Vector3.zero;
            if (cam != null && Physics.Raycast(new Ray(cam.transform.position, cam.transform.forward), out var hit, 200f))
                pos = hit.point + hit.normal * 0.5f;
            var copy = Instantiate(prefab, pos, Quaternion.identity);
            copy.SetActive(true);
            spawned.Push(copy);
            Plugin.Log.LogInfo($"Spawned {prefab.name}");
        }
    }
}
