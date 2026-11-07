package com.winlator.cmod.bulb

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.net.Uri
import android.net.http.SslCertificate
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import java.io.ByteArrayInputStream
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

/**
 * bulb.local inside the app: the dashboard, and retro / HTML5 games, which play in the
 * browser engine. Only the pinned Bulb cloud is trusted; other links open outside.
 */
class BulbWebActivity : Activity() {
    companion object {
        const val EXTRA_PATH = "path"
        private const val PICK_FILES = 71
    }

    private lateinit var web: WebView
    private lateinit var root: FrameLayout
    private var customView: View? = null
    private var customCallback: WebChromeClient.CustomViewCallback? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        BulbCloud.init(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#06070F")) }
        web = WebView(this)
        root.addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        setContentView(root)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            setSupportMultipleWindows(false)
            javaScriptCanOpenWindowsAutomatically = true
            mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            userAgentString = "$userAgentString BulbApp/1.0"
        }
        web.addJavascriptInterface(Bridge(), "BulbNative")
        web.webViewClient = Client()
        web.webChromeClient = Chrome()
        val path = intent.getStringExtra(EXTRA_PATH) ?: "/"
        web.loadUrl(BulbCloud.webUrl(path))
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) immersive()
    }

    @Suppress("DEPRECATION")
    private fun immersive() {
        if (Build.VERSION.SDK_INT >= 30) {
            window.insetsController?.let {
                it.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
        }
    }

    private fun fingerprint(cert: SslCertificate?): String? = runCatching {
        val der = SslCertificate.saveState(cert).getByteArray("x509-certificate") ?: return null
        val x = CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(der)) as X509Certificate
        BulbCloud.fingerprintOf(x)
    }.getOrNull()

    private inner class Client : WebViewClient() {
        @SuppressLint("WebViewClientOnReceivedSslError")
        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            // Only Bulb's own (pinned) self-signed certificate is accepted.
            if (fingerprint(error.certificate) == BulbCloud.pin) handler.proceed() else handler.cancel()
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val u = request.url
            val server = BulbCloud.server?.substringBefore(':')
            if (u.host == server || u.scheme == "blob" || u.scheme == "data" || u.scheme == "about") return false
            runCatching { startActivity(Intent(Intent.ACTION_VIEW, u)) }
            return true
        }

        override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
            val game = url != null && url.contains("/play?")
            requestedOrientation = if (game) ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            else ActivityInfo.SCREEN_ORIENTATION_FULL_USER
        }
    }

    private inner class Chrome : WebChromeClient() {
        override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
            fileCallback?.onReceiveValue(null)
            fileCallback = callback
            val i = params.createIntent().apply {
                type = "*/*"
                if (params.mode == FileChooserParams.MODE_OPEN_MULTIPLE) putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            }
            return try {
                @Suppress("DEPRECATION")
                startActivityForResult(i, PICK_FILES)
                true
            } catch (e: Exception) {
                fileCallback = null
                false
            }
        }

        override fun onShowCustomView(view: View, callback: CustomViewCallback) {
            if (customView != null) { callback.onCustomViewHidden(); return }
            customView = view
            customCallback = callback
            root.addView(view, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
            web.visibility = View.GONE
            immersive()
        }

        override fun onHideCustomView() {
            customView?.let { root.removeView(it) }
            customView = null
            web.visibility = View.VISIBLE
            customCallback?.onCustomViewHidden()
            customCallback = null
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode != PICK_FILES) {
            @Suppress("DEPRECATION")
            super.onActivityResult(requestCode, resultCode, data)
            return
        }
        val result: Array<Uri>? = if (resultCode == RESULT_OK && data != null) {
            val clip = data.clipData
            when {
                clip != null -> Array(clip.itemCount) { clip.getItemAt(it).uri }
                data.data != null -> arrayOf(data.data!!)
                else -> null
            }
        } else null
        fileCallback?.onReceiveValue(result)
        fileCallback = null
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        when {
            customView != null -> (web.webChromeClient as? Chrome)?.onHideCustomView()
            web.canGoBack() -> web.goBack()
            else -> {
                @Suppress("DEPRECATION")
                super.onBackPressed()
            }
        }
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }

    /** window.BulbNative, used by the Bulb OS pages when they run inside the app. */
    private inner class Bridge {
        @JavascriptInterface
        fun switchServer() = runOnUiThread {
            BulbCloud.forget()
            Toast.makeText(this@BulbWebActivity, "Pick a Bulb cloud in Bulb Cloud", Toast.LENGTH_LONG).show()
            finish()
        }

        @JavascriptInterface
        fun setOrientation(o: String) = runOnUiThread {
            requestedOrientation = when (o) {
                "landscape" -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                "portrait" -> ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
                else -> ActivityInfo.SCREEN_ORIENTATION_FULL_USER
            }
        }

        /** "Download & play" on a PC game in the dashboard: back to the Bulb Cloud screen. */
        @JavascriptInterface
        fun openCloudGame(id: String) = runOnUiThread {
            Toast.makeText(this@BulbWebActivity, "Download it from the Bulb Cloud list", Toast.LENGTH_LONG).show()
            finish()
        }
    }
}
