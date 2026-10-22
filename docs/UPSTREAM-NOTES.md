# 上游实测笔记

研究版本：**beets 2.14.1**（2026-10 实测，Python 3.13.14 / Windows）。

这份笔记只记「跑起来之后才发现的事」，每条都带复现方式。它们是 `discrate` 做设计取舍的依据。

---

## 1. 默认配置的体量

`beets/config_default.yaml` 共 207 行，约 90 个配置键。其中 `match.distance_weights` 一项就有 20 个子权重：

```yaml
match:
  distance_weights:
    data_source: 2.0
    artist: 3.0
    album: 3.0
    tracks: 2.0
    track_title: 3.0
    ...
```

插件 74 个，导入交互动作 6 种（`SKIP` / `ASIS` / `TRACKS` / `APPLY` / `ALBUMS` / `RETAG`），另有 3 种重复处理动作。

**结论**：这套配置的表达能力确实强，但它假设使用者愿意先读文档。`discrate` 把它们收敛成 3 个具名档位。

---

## 2. `albumartist` 不会从 `artist` 继承

```python
mf.artist = "周杰伦"; mf.album = "叶惠美"; mf.title = "以父之名"; mf.track = 3
mf.save()
item = Item.from_path(path)
item.albumartist   # → ''  （空）
```

默认路径模板是 `$albumartist/$album%aunique{}/$track $title`，第一段空掉，结果被扔进 `Non-Album/`。

值得注意的是：`Item.__getitem__` 里确实做了 `artist` / `albumartist` 互相兜底（`models.py:249-268`），但那只在**模板求值**时生效，`path_formats` 的查询匹配阶段用的是原始字段。

**结论**：`discrate` 在渲染前显式补齐 `albumartist`，并把这一处标为 `inferred`。

---

## 3. 无标签文件全部撞到同一个名字

```python
for name in ("01.mp3", "02.mp3", "03.mp3"):
    item = Item.from_path(name)   # title='' artist='' album='' track=0
    lib.add(item)
    item.destination(relative_to_libdir=True)
# → b'Non-Album.mp3'   ×3
```

三个不同的文件，目标路径完全相同。如果按这个结果执行 `move`，它们会互相覆盖（或全部失败）。

**结论**：`discrate` 把「没有任何曲名信息」的文件判为 `review`，不落盘，并说明原因。

---

## 4. `Item.destination()` 对专辑型文件选错模板

`Library.path_formats` 的解析结果：

```python
[('default', '$albumartist/$album%aunique{}/$track $title'),
 ('singleton:true', 'Non-Album/$artist/$title'),
 ('comp:true', 'Compilations/$album%aunique{}/$track $title')]
```

`destination()` 的逻辑是：先遍历非 `default` 的模板，用 `parse_query_string` 逐个匹配 Item，第一个命中就用它。

问题在于 `singleton` 是 **Album 级字段**，而 `Item.singleton` 恒为 `True`（表示「这是一首单曲」）。于是：

```python
item.singleton            # → True
item.album                # → '叶惠美'
item.destination()        # → '.../Non-Album/周杰伦/以父之名.mp3'   ← 走了 singleton 模板
item.evaluate_template('$albumartist/$album/$track $title', for_path=True)
# → '周杰伦/叶惠美/03 以父之名'   ← 模板本身是对的
```

**同一个对象，`destination()` 和 `evaluate_template()` 给出的答案不一样。** 这不是 bug，是 `Item.destination()` 只服务 singleton 导入的架构设计 —— 但 API 表面看起来是通用的。

**结论**：`discrate` 不用 `Item.destination()`，自己按「有没有专辑」选模板。

---

## 5. `sanitize_path()` 是给路径片段用的

```python
sanitize_path("AC/DC")        # → 'AC\\DC'   （Windows：两级目录）
sanitize_path('a<b>c:d')      # → 'a_b_c_d'
sanitize_path("CON")          # → 'CON'      （Windows 保留名，未处理）
sanitize_path("name. ")       # → 'name.'    （尾部点保留）
```

前两条的根因是 `sanitize_path` 先 `components(path)` 按分隔符拆开，再对每段做替换，最后 `os.path.join` 拼回。它设计上就接受「带层级的路径片段」。

**结论**：传给它之前必须先自己把 `/` `\` 换掉；保留名和尾部点要另补。

---

## 6. `try_write()` 会把整个 Item 写回

```python
item = Item.from_path(path)   # 读全字段
item.title = "只写标题"
item.try_write()
# → artist / album 变成空字符串
```

如果按「造一个新 Item，只设需要的字段，再写回」的思路做，会**清空文件里原有的标签**。

**结论**：`discrate` 永远 `Item.from_path()` 之后改字段再写，绝不用空 Item。

---

## 7. 多值字段改名了

2.x 把 `genre` / `composer` / `lyricist` 改成了多值字段。空 Item 上访问 `item.genre` 抛 `KeyError: 'genre'`，字段名实际是 `genres`。

`Item.keys()` 里能看到 `albumtypes` / `albumartists` 这类复数名，但 `genre` 这个单数名在空对象上会直接报错。

**结论**：`discrate` 只用 `artist` / `albumartist` / `album` / `title` / `track` / `disc` / `year` 这七个稳定字段，不碰多值字段。

---

## 8. `Library` 初始化会刷一堆迁移备份日志

每次 `Library(...)` 首次建库，都会往 stderr 打十来行：

```
Created database backup at: '...-before-items-multi_genre_field.bak'
...
```

对命令行工具来说这是噪音。`discrate` 通过把 beets 的 logger 调到 `CRITICAL` 来压掉，同时把 `BEETSDIR` 指到私有目录，避免读用户已有的配置。

---

## 9. 没有官方的「非交互 + 可预演」路径

`beet import -q`（quiet）能跳过所有问答，但它同时意味着：

- 没有预演（`-p` 只打印，不落盘，但也不产报告）
- 判定理由不可见
- 没有回滚机制
- 重复处理按 `duplicate_action` 一次性决定

**结论**：这正是 `discrate` 的立足点 —— 把「闭眼执行」换成「睁眼执行 + 可撤销」。
