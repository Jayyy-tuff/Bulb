package com.winlator.cmod.bulb

import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.fragment.app.Fragment
import com.winlator.cmod.XServerDisplayActivity
import com.winlator.cmod.container.Container
import com.winlator.cmod.container.ContainerManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** The Bulb Cloud screen: find bulb.local, sign in, download PC games and play everything. */
class BulbCloudFragment : Fragment() {
    private val amber = Color.parseColor("#FFD23F")
    private val cyan = Color.parseColor("#3FF0FF")
    private val dim = Color.parseColor("#9AA3C7")
    private val card = Color.parseColor("#161A34")
    private val line = Color.parseColor("#3A4270")

    private var scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private lateinit var content: LinearLayout
    private var discovery: BulbCloud.Discovery? = null
    private var games: List<BulbCloud.Game> = emptyList()
    private var error: String? = null
    private var loading = false

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, state: Bundle?): View {
        BulbCloud.init(requireContext())
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        val scroll = ScrollView(requireContext()).apply { isFillViewport = true }
        content = LinearLayout(requireContext()).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(16), dp(18), dp(32))
        }
        scroll.addView(content)
        return scroll
    }

    override fun onViewCreated(view: View, state: Bundle?) {
        requireActivity().title = "Bulb Cloud"
        scope.launch { BulbCloudDownloads.progress.collectLatest { if (BulbCloud.signedIn) render() } }
        if (BulbCloud.signedIn) refresh() else render()
    }

    override fun onResume() {
        super.onResume()
        if (BulbCloud.signedIn && games.isNotEmpty()) render()
    }

    override fun onDestroyView() {
        discovery?.stop()
        discovery = null
        scope.cancel()
        super.onDestroyView()
    }

    // ------------------------------------------------------------------ screens

    private fun render() {
        if (!isAdded) return
        content.removeAllViews()
        when {
            BulbCloud.server == null || BulbCloud.pin == null -> connectScreen()
            BulbCloud.token == null -> loginScreen()
            else -> libraryScreen()
        }
    }

    private fun connectScreen() {
        content.addView(title("Bulb Cloud"))
        content.addView(text("Connect to your Bulb OS cloud on this Wi-Fi. That's where your games and saves live.", dim))
        val status = text("Searching your Wi-Fi…", cyan).also { it.setPadding(0, dp(18), 0, dp(6)) }
        content.addView(status)
        val found = LinearLayout(requireContext()).apply { orientation = LinearLayout.VERTICAL }
        content.addView(found)
        val seen = HashSet<String>()
        discovery?.stop()
        discovery = BulbCloud.Discovery(requireContext()) { name, addr ->
            if (!isAdded || !seen.add(addr)) return@Discovery
            status.text = "Found ${seen.size} Bulb cloud${if (seen.size > 1) "s" else ""}"
            found.addView(button("$name\n$addr", primary = true) { connect(addr) }, full(top = 8))
        }.also { it.start() }
        content.addView(button("Try bulb.local") { connect("bulb.local") }, full(top = 16))
        content.addView(text("Or type its address (shown on the Bulb OS console)", dim).also { it.setPadding(0, dp(22), 0, dp(6)) })
        val addr = input("192.168.1.20", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        content.addView(addr, full())
        content.addView(button("Connect", primary = true) {
            val a = addr.text.toString().trim().removePrefix("https://").removePrefix("http://").substringBefore('/')
            if (a.isNotEmpty()) connect(a)
        }, full(top = 10))
        error?.let { content.addView(text(it, Color.parseColor("#FF5A6E")).also { t -> t.setPadding(0, dp(14), 0, 0) }) }
    }

    private fun connect(address: String) {
        error = null
        toast("Connecting to $address…")
        scope.launch {
            val probe = try {
                withContext(Dispatchers.IO) { BulbCloud.probe(address) }
            } catch (e: Exception) {
                error = "Couldn't reach $address (${e.message}). Is the Bulb OS VM running, with a bridged network?"
                render()
                return@launch
            }
            if (!isAdded) return@launch
            AlertDialog.Builder(requireContext())
                .setTitle("Trust ${probe.name}?")
                .setMessage("Bulb OS uses its own certificate. Check that this fingerprint matches `bulbctl fingerprint` on the box:\n\n${BulbCloud.pretty(probe.fingerprint)}")
                .setPositiveButton("Trust") { _, _ ->
                    discovery?.stop()
                    BulbCloud.trust(address, probe.fingerprint)
                    if (!probe.setup) toast("This cloud has no owner yet. Create one at https://$address in a browser.")
                    render()
                }
                .setNegativeButton("Cancel", null)
                .show()
        }
    }

    private fun loginScreen() {
        content.addView(title("Sign in"))
        content.addView(text("to ${BulbCloud.server}. Use the account you made on bulb.local.", dim))
        val user = input("Username", InputType.TYPE_CLASS_TEXT)
        val pass = input("Password", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        content.addView(user, full(top = 18))
        content.addView(pass, full(top = 10))
        content.addView(button("Sign in", primary = true) {
            val u = user.text.toString().trim()
            val p = pass.text.toString()
            scope.launch {
                try {
                    withContext(Dispatchers.IO) { BulbCloud.login(u, p) }
                    refresh()
                } catch (e: Exception) {
                    toast(e.message ?: "Sign-in failed")
                }
            }
        }, full(top = 16))
        content.addView(button("No account yet? Create one in the browser") { openWeb("/") }, full(top = 10))
        content.addView(button("Connect to a different cloud") { BulbCloud.forget(); render() }, full(top = 10))
    }

    private fun refresh() {
        loading = true
        render()
        scope.launch {
            try {
                games = withContext(Dispatchers.IO) { BulbCloud.library() }
                error = null
            } catch (e: Exception) {
                error = e.message ?: "Couldn't load the library"
            }
            loading = false
            render()
        }
    }

    private fun libraryScreen() {
        val ctx = requireContext()
        val head = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        head.addView(LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            addView(title(BulbCloud.cloudName))
            addView(text("${BulbCloud.userName} · ${BulbCloud.server}", dim))
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        content.addView(head)
        val actions = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
        actions.addView(button("↻ Refresh") { refresh() }, weight())
        actions.addView(button("bulb.local") { openWeb("/") }, weight(left = 8))
        actions.addView(button("Sign out") {
            BulbCloud.signOut(); games = emptyList(); render()
        }, weight(left = 8))
        content.addView(actions, full(top = 12))
        if (loading) content.addView(text("Loading your library…", cyan).also { it.setPadding(0, dp(16), 0, 0) })
        error?.let { content.addView(text(it, Color.parseColor("#FF5A6E")).also { t -> t.setPadding(0, dp(14), 0, 0) }) }

        val installed = BulbCloudDownloads.installed(ctx)
        val pc = games.filter { it.kind == "pc" }
        val other = games.filter { it.kind != "pc" }
        if (pc.isNotEmpty()) {
            content.addView(section("PC games"))
            pc.forEach { content.addView(pcRow(it, installed[it.id]), full(top = 10)) }
        }
        if (other.isNotEmpty()) {
            content.addView(section("Retro & web games"))
            other.forEach { g ->
                content.addView(row(g.title, if (g.kind == "rom") g.system.uppercase() + " · plays instantly" else "HTML5 · plays instantly", null,
                    listOf(button("▶ Play", primary = true) { openWeb(if (g.kind == "rom") "/play?id=${g.id}" else "/#library") })), full(top = 10))
            }
        }
        if (!loading && games.isEmpty() && error == null) {
            content.addView(text("No games in the cloud yet. Upload some at https://${BulbCloud.server} (the + button).", dim).also { it.setPadding(0, dp(20), 0, 0) })
        }
    }

    private fun pcRow(g: BulbCloud.Game, inst: BulbCloudDownloads.Installed?): View {
        val p = BulbCloudDownloads.progress.value[g.id]
        val running = BulbCloudDownloads.isRunning(g.id)
        val buttons = ArrayList<View>()
        val sub: String
        var bar: Int? = null
        when {
            running -> {
                val pct = if (p != null && p.total > 0) (p.done * 100 / p.total).toInt() else 0
                sub = "Downloading… $pct% · ${size(p?.done ?: 0)} of ${size(p?.total ?: g.size)}"
                bar = pct
                buttons += button("Pause") { BulbCloudDownloads.cancel(g.id); render() }
            }
            inst != null -> {
                sub = "On this phone · ${size(g.size)}"
                if (inst.exe.isNotEmpty() || g.exe.isNotEmpty()) buttons += button("▶ Play", primary = true) { play(g, inst, installer = false) }
                if (inst.installer.isNotEmpty()) buttons += button("Run installer") { play(g, inst, installer = true) }
                buttons += button("Update") { BulbCloudDownloads.start(requireContext(), g); render() }
                buttons += button("Delete") { confirmDelete(g) }
            }
            else -> {
                sub = if (p?.error != null) "${p.error} · tap to resume" else "${size(g.size)} · download to play offline"
                buttons += button(if (p?.error != null) "Resume" else "⬇ Download", primary = true) {
                    BulbCloudDownloads.start(requireContext(), g); render()
                }
            }
        }
        val cover = inst?.cover?.takeIf { it.isNotEmpty() }?.let { File(it) }
        return row(g.title, sub, cover, buttons, bar)
    }

    private fun confirmDelete(g: BulbCloud.Game) {
        AlertDialog.Builder(requireContext())
            .setTitle("Delete ${g.title} from this phone?")
            .setMessage("It stays on your Bulb cloud, so you can download it again later.")
            .setPositiveButton("Delete") { _, _ -> BulbCloudDownloads.delete(requireContext(), g.id); render() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ------------------------------------------------------------------ playing

    private fun play(g: BulbCloud.Game, inst: BulbCloudDownloads.Installed, installer: Boolean) {
        val ctx = requireContext()
        scope.launch {
            val containers = withContext(Dispatchers.IO) { ContainerManager(ctx).containers.toList() }
            if (containers.isEmpty()) {
                toast("Create a container first: menu → Containers → +")
                return@launch
            }
            val prefs = ctx.getSharedPreferences("bulb_cloud_launch", Context.MODE_PRIVATE)
            val saved = containers.firstOrNull { it.id == prefs.getInt("container_${g.id}", -1) }
            val chosen = saved ?: containers.singleOrNull()
            if (chosen != null) {
                launchIn(inst, chosen, installer)
                return@launch
            }
            AlertDialog.Builder(ctx)
                .setTitle("Play ${g.title} in which container?")
                .setItems(containers.map { it.name }.toTypedArray()) { _, i ->
                    prefs.edit().putInt("container_${g.id}", containers[i].id).apply()
                    scope.launch { launchIn(inst, containers[i], installer) }
                }
                .show()
        }
    }

    private suspend fun launchIn(inst: BulbCloudDownloads.Installed, container: Container, installer: Boolean) {
        val ctx = context ?: return
        toast("Preparing ${inst.title}…")
        val shortcut = try {
            BulbCloudDownloads.prepare(ctx, inst, container, installer)
        } catch (e: Exception) {
            toast(e.message ?: "Couldn't prepare the game")
            return
        }
        val intent = Intent(ctx, XServerDisplayActivity::class.java)
            .putExtra("container_id", shortcut.container.id)
            .putExtra("shortcut_path", shortcut.file.path)
            .putExtra("shortcut_name", shortcut.name)
            .putExtra("disableXinput", shortcut.getExtra("disableXinput", "0"))
        startActivity(intent)
        if (installer) toast("When setup finishes, pick the installed game's .exe on bulb.local, then tap Update here.")
    }

    private fun openWeb(path: String) {
        startActivity(Intent(requireContext(), BulbWebActivity::class.java).putExtra(BulbWebActivity.EXTRA_PATH, path))
    }

    // ------------------------------------------------------------------ views

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun title(s: String) = TextView(requireContext()).apply {
        text = s; textSize = 24f; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD
    }

    private fun section(s: String) = TextView(requireContext()).apply {
        text = s.uppercase(); textSize = 12f; letterSpacing = 0.15f; setTextColor(dim); typeface = Typeface.DEFAULT_BOLD
        setPadding(0, dp(26), 0, 0)
    }

    private fun text(s: String, color: Int) = TextView(requireContext()).apply { text = s; textSize = 14f; setTextColor(color) }

    private fun rounded(fill: Int, stroke: Int) = GradientDrawable().apply {
        setColor(fill); cornerRadius = dp(14).toFloat(); setStroke(dp(1), stroke)
    }

    private fun button(label: String, primary: Boolean = false, onClick: () -> Unit) = Button(requireContext()).apply {
        text = label; isAllCaps = false; textSize = 14f; typeface = Typeface.DEFAULT_BOLD
        setTextColor(if (primary) Color.parseColor("#1A1200") else Color.WHITE)
        background = rounded(if (primary) amber else card, if (primary) amber else line)
        setPadding(dp(12), dp(10), dp(12), dp(10))
        minHeight = dp(44)
        setOnClickListener { onClick() }
    }

    private fun input(hint: String, type: Int) = EditText(requireContext()).apply {
        this.hint = hint; inputType = type; setSingleLine(true)
        setTextColor(Color.WHITE); setHintTextColor(Color.parseColor("#646D93"))
        background = rounded(Color.parseColor("#0B0D1C"), line)
        setPadding(dp(14), dp(12), dp(14), dp(12))
    }

    private fun row(titleText: String, sub: String, cover: File?, buttons: List<View>, percent: Int? = null): View {
        val ctx = requireContext()
        val box = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(card, line)
            setPadding(dp(14), dp(12), dp(14), dp(12))
        }
        val top = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        if (cover != null && cover.isFile) {
            val img = ImageView(ctx).apply {
                setImageBitmap(BitmapFactory.decodeFile(cover.path))
                scaleType = ImageView.ScaleType.CENTER_CROP
            }
            top.addView(img, LinearLayout.LayoutParams(dp(48), dp(64)).apply { rightMargin = dp(12) })
        }
        top.addView(LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(ctx).apply { text = titleText; textSize = 16f; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD })
            addView(text(sub, dim))
        }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(top)
        if (percent != null) {
            val bar = ProgressBar(ctx, null, android.R.attr.progressBarStyleHorizontal)
            bar.max = 100
            bar.progress = percent
            box.addView(bar, full(top = 8))
        }
        if (buttons.isNotEmpty()) {
            val row = LinearLayout(ctx).apply { orientation = LinearLayout.HORIZONTAL }
            buttons.forEachIndexed { i, b -> row.addView(b, weight(left = if (i == 0) 0 else 8)) }
            box.addView(row, full(top = 10))
        }
        return box
    }

    private fun full(top: Int = 0) = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        .apply { topMargin = dp(top) }

    private fun weight(left: Int = 0) = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        .apply { leftMargin = dp(left) }

    private fun size(n: Long): String {
        if (n <= 0) return "0 B"
        val u = listOf("B", "KB", "MB", "GB", "TB")
        var v = n.toDouble(); var i = 0
        while (v >= 1024 && i < u.size - 1) { v /= 1024; i++ }
        return if (i == 0) "$n B" else "%.1f %s".format(v, u[i])
    }

    private fun toast(s: String) {
        context?.let { Toast.makeText(it, s, Toast.LENGTH_LONG).show() }
    }
}
