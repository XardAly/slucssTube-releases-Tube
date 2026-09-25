package com.xard.ytsystem.update

import com.xard.ytsystem.api.AndroidManifest

object ReleasePolicy {
    fun key(manifest: AndroidManifest): String = manifest.releaseId.ifBlank { "android-${manifest.latestVersionCode}" }
    fun required(manifest: AndroidManifest, installed: Int): Boolean = installed < manifest.minimumVersionCode ||
        (manifest.updatePolicy == "mandatory" && installed < manifest.latestVersionCode)
    fun shouldNotify(installed: Int, offered: Int, release: String, previous: String, previousCode: Int): Boolean =
        offered > installed && offered > previousCode && release != previous
}
