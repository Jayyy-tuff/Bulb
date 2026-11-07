package com.winlator.cmod.bulb

import android.content.Context
import android.content.SharedPreferences
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Connection to a Bulb OS cloud (https://bulb.local on the home network).
 *
 * Bulb OS uses a self-signed certificate, so the app pins it on first connect
 * ("trust on first use"): the user confirms its SHA-256 fingerprint once (it matches
 * `bulbctl fingerprint` on the box) and every later connection must present the same one.
 */
object BulbCloud {
    private const val TAG = "BulbCloud"
    const val SERVICE_TYPE = "_bulb._tcp."
    private val JSON = "application/json".toMediaType()

    private lateinit var prefs: SharedPreferences

    fun init(context: Context) {
        if (!::prefs.isInitialized) prefs = context.applicationContext.getSharedPreferences("bulb_cloud", Context.MODE_PRIVATE)
    }

    val server: String? get() = prefs.getString("server", null)
    val token: String? get() = prefs.getString("token", null)
    val pin: String? get() = prefs.getString("pin", null)
    val cloudName: String get() = prefs.getString("cloud_name", null) ?: "Bulb"
    val userName: String get() = prefs.getString("user_name", null) ?: ""
    val signedIn: Boolean get() = server != null && token != null && pin != null

    fun baseUrl(server: String = this.server ?: "bulb.local") = "https://$server"

    fun signOut() {
        prefs.edit().remove("token").remove("user_name").apply()
    }

    fun forget() {
        prefs.edit().clear().apply()
    }

    // ------------------------------------------------------------------ TLS pinning

    class Probe(val fingerprint: String, val name: String, val setup: Boolean)

    fun fingerprintOf(cert: X509Certificate): String =
        MessageDigest.getInstance("SHA-256").digest(cert.encoded).joinToString("") { "%02X".format(it) }

    fun pretty(fp: String): String = fp.chunked(2).chunked(8).joinToString("\n") { it.joinToString(":") }

    private class PinTrust(private val expected: String?) : X509TrustManager {
        @Volatile var seen: String? = null
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            val fp = fingerprintOf(chain[0])
            seen = fp
            if (expected != null && fp != expected) {
                throw java.security.cert.CertificateException(
                    "This Bulb cloud's certificate changed. If you reinstalled Bulb OS, connect to it again."
                )
            }
        }
        override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
    }

    private fun client(trust: PinTrust, timeoutSec: Long = 30): OkHttpClient {
        val ssl = SSLContext.getInstance("TLS")
        ssl.init(null, arrayOf(trust), SecureRandom())
        return OkHttpClient.Builder()
            .sslSocketFactory(ssl.socketFactory, trust)
            .hostnameVerifier { _, _ -> true } // the pinned fingerprint is what we check
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(timeoutSec, TimeUnit.SECONDS)
            .build()
    }

    @Volatile private var cached: OkHttpClient? = null
    @Volatile private var cachedPin: String? = null

    /** HTTP client that only talks to the pinned Bulb cloud. */
    fun http(): OkHttpClient {
        val p = pin ?: throw IOException("Not connected to a Bulb cloud")
        cached?.let { if (cachedPin == p) return it }
        return client(PinTrust(p), 120).also { cached = it; cachedPin = p }
    }

    /** First contact: reads the certificate fingerprint and the cloud's name. */
    fun probe(server: String): Probe {
        val trust = PinTrust(null)
        client(trust, 15).newCall(Request.Builder().url(baseUrl(server) + "/api/status").build()).execute().use { r ->
            val body = JSONObject(r.body.string())
            val fp = trust.seen ?: throw IOException("No certificate from $server")
            return Probe(fp, body.optString("name", "Bulb"), body.optBoolean("setup", false))
        }
    }

    fun trust(server: String, fingerprint: String) {
        prefs.edit().putString("server", server).putString("pin", fingerprint).remove("token").apply()
        cached = null
    }

    // ------------------------------------------------------------------ API

    class ApiError(message: String, val status: Int) : IOException(message)

    private fun request(path: String): Request.Builder {
        val b = Request.Builder().url(baseUrl() + path)
        token?.let { b.header("Authorization", "Bearer $it") }
        return b
    }

    private fun call(req: Request): JSONObject {
        http().newCall(req).execute().use { r ->
            val text = r.body.string()
            val json = runCatching { JSONObject(text) }.getOrElse { JSONObject() }
            if (r.code == 401) signOut()
            if (!r.isSuccessful) throw ApiError(json.optString("error", "Bulb answered ${r.code}"), r.code)
            return json
        }
    }

    fun get(path: String): JSONObject = call(request(path).get().build())

    fun post(path: String, body: JSONObject): JSONObject =
        call(request(path).post(body.toString().toRequestBody(JSON)).build())

    fun login(username: String, password: String) {
        val device = listOf(Build.MANUFACTURER.replaceFirstChar { it.uppercase() }, Build.MODEL).distinct().joinToString(" ")
        val r = post("/api/auth/token", JSONObject().put("username", username).put("password", password).put("device", device))
        prefs.edit()
            .putString("token", r.getString("token"))
            .putString("user_name", r.getJSONObject("user").optString("display"))
            .putString("cloud_name", r.optString("name", "Bulb"))
            .apply()
    }

    data class Game(
        val id: String, val kind: String, val title: String, val system: String,
        val size: Long, val cover: Long, val exe: String, val installer: String,
    )

    fun library(): List<Game> {
        val arr: JSONArray = get("/api/library").getJSONArray("games")
        return (0 until arr.length()).map { i ->
            val g = arr.getJSONObject(i)
            Game(
                g.getString("id"), g.optString("kind"), g.optString("title"), g.optString("system"),
                g.optLong("size"), g.optLong("cover"), g.optString("exe"), g.optString("installer"),
            )
        }
    }

    fun manifest(id: String): JSONObject = get("/api/games/$id/manifest")

    /** Downloads [path] into [out], resuming a partial ".part" file. [onBytes] gets each chunk's size. */
    fun download(path: String, out: File, expected: Long, onBytes: (Long) -> Unit) {
        if (out.isFile && out.length() == expected) { onBytes(expected); return }
        out.parentFile?.mkdirs()
        val part = File(out.path + ".part")
        var have = if (part.isFile) part.length() else 0L
        if (have > expected) { part.delete(); have = 0 }
        if (have > 0) onBytes(have)
        val req = request(path).apply { if (have > 0) header("Range", "bytes=$have-") }.build()
        http().newCall(req).execute().use { r ->
            if (r.code == 401) signOut()
            if (!r.isSuccessful) throw ApiError("Download failed (${r.code})", r.code)
            val append = r.code == 206
            if (!append && have > 0) { onBytes(-have); have = 0 }
            r.body.byteStream().use { input ->
                java.io.FileOutputStream(part, append).use { output ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        onBytes(n.toLong())
                    }
                }
            }
        }
        if (part.length() != expected && expected > 0) throw IOException("${out.name} is incomplete; try again")
        if (out.exists()) out.delete()
        if (!part.renameTo(out)) throw IOException("Couldn't save ${out.name}")
    }

    fun downloadTo(path: String, out: File): Boolean {
        http().newCall(request(path).get().build()).execute().use { r ->
            if (!r.isSuccessful) return false
            out.parentFile?.mkdirs()
            r.body.byteStream().use { i -> out.outputStream().use { o -> i.copyTo(o) } }
            return true
        }
    }

    fun upload(path: String, file: File) {
        val req = request(path).put(file.asRequestBody("application/octet-stream".toMediaType())).build()
        http().newCall(req).execute().use { r -> if (!r.isSuccessful) throw ApiError("Upload failed (${r.code})", r.code) }
    }

    /** A link that signs the app's web view in and opens [next] (retro / HTML5 games, the dashboard). */
    fun webUrl(next: String): String {
        val t = token ?: return baseUrl() + next
        return baseUrl() + "/auth/web?t=" + java.net.URLEncoder.encode(t, "UTF-8") +
            "&next=" + java.net.URLEncoder.encode(next, "UTF-8")
    }

    // ------------------------------------------------------------------ discovery

    /** Finds Bulb clouds on the Wi-Fi (Bulb OS advertises _bulb._tcp). Call [stop] on the result. */
    class Discovery(context: Context, private val onFound: (name: String, address: String) -> Unit) {
        private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        private val main = Handler(Looper.getMainLooper())
        private val queue = ArrayDeque<NsdServiceInfo>()
        private var resolving = false
        private var listener: NsdManager.DiscoveryListener? = null

        fun start() {
            val l = object : NsdManager.DiscoveryListener {
                override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { Log.w(TAG, "discovery failed $errorCode") }
                override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
                override fun onDiscoveryStarted(serviceType: String) {}
                override fun onDiscoveryStopped(serviceType: String) {}
                override fun onServiceFound(info: NsdServiceInfo) { synchronized(queue) { queue.add(info) }; next() }
                override fun onServiceLost(info: NsdServiceInfo) {}
            }
            listener = l
            try { nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l) } catch (e: Exception) { Log.w(TAG, "discovery", e) }
        }

        fun stop() {
            listener?.let { runCatching { nsd.stopServiceDiscovery(it) } }
            listener = null
        }

        @Suppress("DEPRECATION")
        private fun next() {
            val info = synchronized(queue) {
                if (resolving || queue.isEmpty()) return
                resolving = true
                queue.removeFirst()
            }
            nsd.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    synchronized(queue) { resolving = false }
                    main.postDelayed({ next() }, 300)
                }
                override fun onServiceResolved(s: NsdServiceInfo) {
                    synchronized(queue) { resolving = false }
                    val ip = s.host?.hostAddress
                    if (ip != null && !ip.contains(':')) {
                        val addr = if (s.port == 443 || s.port <= 0) ip else "$ip:${s.port}"
                        main.post { onFound(s.serviceName.replace("\\032", " "), addr) }
                    }
                    main.post { next() }
                }
            })
        }
    }
}
