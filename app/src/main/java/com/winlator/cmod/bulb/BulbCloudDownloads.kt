package com.winlator.cmod.bulb

import android.content.Context
import android.os.Environment
import android.util.Log
import com.winlator.cmod.container.Container
import com.winlator.cmod.container.Shortcut
import com.winlator.cmod.steam.SteamControls
import com.winlator.cmod.store.StoreShortcuts
import com.winlator.cmod.store.WineMono
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException

/**
 * PC games from the Bulb cloud: downloads them to Downloads/Bulb/Cloud, remembers what is
 * installed, and turns a downloaded game into a container shortcut (like the Steam and GOG
 * integrations do) so it runs with Wine on the phone.
 */
object BulbCloudDownloads {
    private const val TAG = "BulbCloudDownloads"

    val libraryRoot: File
        get() = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "Bulb/Cloud")

    data class Installed(
        val id: String, val title: String, val dir: String, val exe: String,
        val installer: String, val args: String, val version: Long, val cover: String,
    )

    data class Progress(val id: String, val title: String, val done: Long, val total: Long,
                        val error: String? = null, val running: Boolean = true)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val running = HashMap<String, Job>()
    private val _progress = MutableStateFlow<Map<String, Progress>>(emptyMap())
    val progress: StateFlow<Map<String, Progress>> = _progress

    private fun recordFile(context: Context) = File(context.filesDir, "bulb/installed.json")

    fun installed(context: Context): Map<String, Installed> = synchronized(this) {
        val f = recordFile(context)
        if (!f.isFile) return emptyMap()
        val json = runCatching { JSONObject(f.readText()) }.getOrElse { JSONObject() }
        json.keys().asSequence().associateWith { id ->
            val o = json.getJSONObject(id)
            Installed(id, o.optString("title"), o.optString("dir"), o.optString("exe"), o.optString("installer"),
                o.optString("args"), o.optLong("version"), o.optString("cover"))
        }.filterValues { File(it.dir).isDirectory }
    }

    private fun save(context: Context, inst: Installed?, removeId: String? = null) = synchronized(this) {
        val f = recordFile(context)
        f.parentFile?.mkdirs()
        val json = if (f.isFile) runCatching { JSONObject(f.readText()) }.getOrElse { JSONObject() } else JSONObject()
        removeId?.let { json.remove(it) }
        inst?.let {
            json.put(it.id, JSONObject().put("title", it.title).put("dir", it.dir).put("exe", it.exe)
                .put("installer", it.installer).put("args", it.args).put("version", it.version).put("cover", it.cover))
        }
        f.writeText(json.toString(1))
    }

    fun isRunning(id: String) = synchronized(running) { running[id]?.isActive == true }

    private fun publish(p: Progress) {
        _progress.value = _progress.value + (p.id to p)
    }

    /** Downloads (or updates) [game] from the cloud. Safe to call again to resume. */
    fun start(context: Context, game: BulbCloud.Game) {
        val app = context.applicationContext
        synchronized(running) {
            if (running[game.id]?.isActive == true) return
            running[game.id] = scope.launch { download(app, game) }
        }
    }

    fun cancel(id: String) {
        synchronized(running) { running.remove(id)?.cancel() }
        _progress.value[id]?.let { publish(it.copy(running = false, error = "Paused")) }
    }

    private suspend fun download(context: Context, game: BulbCloud.Game) {
        var done = 0L
        var total = 0L
        try {
            publish(Progress(game.id, game.title, 0, 0))
            val m = BulbCloud.manifest(game.id)
            val files = m.getJSONArray("files")
            total = m.optLong("total")
            val dir = installDir(context, game)
            var last = 0L
            for (i in 0 until files.length()) {
                val f = files.getJSONObject(i)
                val rel = f.getString("path").removePrefix("files/")
                val out = File(dir, rel)
                if (!out.canonicalPath.startsWith(dir.canonicalPath + File.separator)) throw IOException("Bad file path in game")
                coroutineContextCheck()
                BulbCloud.download(f.getString("url"), out, f.optLong("size")) { n ->
                    if (synchronized(running) { running[game.id] } == null) throw CancellationException("Paused")
                    done += n
                    val now = System.currentTimeMillis()
                    if (now - last > 300) {
                        last = now
                        publish(Progress(game.id, game.title, done, total))
                    }
                }
            }
            var cover = ""
            if (m.optString("cover").isNotEmpty()) {
                val c = File(context.filesDir, "bulb/covers/${game.id}.png")
                if (runCatching { BulbCloud.downloadTo(m.getString("cover"), c) }.getOrDefault(false)) cover = c.path
            }
            save(context, Installed(game.id, m.optString("title", game.title), dir.path,
                m.optString("exe").removePrefix("files/"), m.optString("installer").removePrefix("files/"),
                m.optString("args"), m.optLong("version"), cover))
            publish(Progress(game.id, game.title, total, total, running = false))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.w(TAG, "Download of ${game.id} failed", e)
            publish(Progress(game.id, game.title, done, total, error = e.message ?: e.javaClass.simpleName, running = false))
        } finally {
            synchronized(running) { running.remove(game.id) }
        }
    }

    private suspend fun coroutineContextCheck() = withContext(Dispatchers.IO) { ensureActive() }

    private fun installDir(context: Context, game: BulbCloud.Game): File {
        installed(context)[game.id]?.let { return File(it.dir) }
        val safe = game.title.replace(Regex("[\\\\/:*?\"<>|]"), "").trim().ifEmpty { "Game" }
        var dir = File(libraryRoot, safe)
        if (dir.exists()) dir = File(libraryRoot, "$safe (${game.id.take(4)})")
        return dir.apply { mkdirs() }
    }

    fun delete(context: Context, id: String) {
        cancel(id)
        installed(context)[id]?.let { File(it.dir).deleteRecursively() }
        save(context, null, removeId = id)
        _progress.value = _progress.value - id
    }

    /**
     * Writes the game's shortcut in [container]. [installer] = run the game's installer
     * instead (its own "Install …" shortcut), for uploads that are setup programs.
     */
    suspend fun prepare(context: Context, inst: Installed, container: Container, installer: Boolean): Shortcut =
        withContext(Dispatchers.IO) {
            val dir = File(inst.dir)
            val rel = if (installer) inst.installer else inst.exe
            if (rel.isEmpty()) throw IOException(
                if (installer) "This game has no installer." else "Pick the game's .exe on bulb.local (Library → the game → Game .exe)."
            )
            val exe = File(dir, rel)
            if (!exe.isFile) throw IOException("${exe.name} is missing. Download the game again.")
            StoreShortcuts.ensureDrive(container, dir, libraryRoot)
            if (WineMono.isDotNet(exe)) WineMono.ensure(context, container)
            val name = if (installer) "Install ${inst.title}" else inst.title
            val shortcut = StoreShortcuts.writeShortcut(
                context, container, "bulb", if (installer) "${inst.id}-setup" else inst.id, name,
                exe, if (installer) "" else inst.args, exe,
            ) { SteamControls.decide(padSupport = false, wasd = false, mouseRank = -1, actionRank = -1, gameExe = exe) }
            val cover = inst.cover.takeIf { it.isNotEmpty() }?.let { File(it) }?.takeIf { it.isFile }
            StoreShortcuts.copyArtwork(shortcut, portrait = cover, hero = null, logo = null, exe = exe)
            shortcut
        }
}
