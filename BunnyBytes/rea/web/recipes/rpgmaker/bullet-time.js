/*:
 * @target MZ MV
 * @plugindesc {{NAME}}: bullet time (slow motion) on a key. Rocsy REA mod.
 * @help Press T to slow the game to 30% speed and again to return to normal.
 */
(() => {
  "use strict";
  const SLOW = 0.3;
  let active = false;
  Input.keyMapper[84] = "rocsyBulletTime"; // T
  const log = (msg) => console.log(`[{{NAME}}] ${msg}`);

  // MZ: the main loop gets a frame delta; scale it.
  if (typeof SceneManager.update === "function" && SceneManager.update.length >= 1) {
    const update = SceneManager.update;
    SceneManager.update = function (deltaTime) {
      update.call(this, active ? deltaTime * SLOW : deltaTime);
    };
  }
  // MV: frames run off an accumulator of real time; feed it scaled time.
  if (typeof SceneManager.updateMain === "function" && "_accumulator" in SceneManager) {
    const updateMain = SceneManager.updateMain;
    SceneManager.updateMain = function () {
      if (active && this._currentTime !== undefined) {
        const now = this._getTimeInMsWithoutMobileSafari ? this._getTimeInMsWithoutMobileSafari() : performance.now();
        const elapsed = (now - this._currentTime) / 1000;
        this._currentTime = now - elapsed * 1000 * SLOW;
      }
      updateMain.call(this);
    };
  }
  const updateInput = Scene_Base.prototype.update;
  Scene_Base.prototype.update = function () {
    if (Input.isTriggered("rocsyBulletTime")) { active = !active; log(`bullet time ${active ? "on" : "off"}`); }
    updateInput.call(this);
  };
  log("loaded: press T for bullet time");
})();
