# 按任务改写

这是针对 Qwen-Image 的原创工作流整理，参考公开 PE 的任务区分和输出契约；未打包官方长系统提示词或权重。保持用户意图比复刻模板句数、长度更重要。

## 文生图

确定媒介、主体、背景和主要关系。主体图从背景、姿态、表情与服装写到接触物和边缘构图；版式图按实际阅读顺序写位置和文字。只补充对执行有用的细节，简短完整的要求无需硬扩成四五百词。

写可见几何关系：坐在哪个面上、哪只手接触何物、腿向哪里延伸、脚落在哪里、镜头从哪边看、身体哪些部分完整入镜。不凭空指定用户没要求的左右侧；为消除歧义而补充时保持全篇一致。

交代光照来源、方向、软硬和色彩。复古风可由环境、材质和服装建立；用户要求中性色时不添加橙黄滤镜。亲密人像以身体语言和视线为主体时，不用大碗、大托盘等遮住身体。

描述画面本身，而不是重复执行说明。例：“不要裁脚”落实为双脚与脚下空间完整可见。并非所有负面要求都能安全删除；不能为追求全肯定句而损失意图。

## 画面文字

- 清点所有目标文字并逐字引用；标题、价格、单位、图表轴标签都算文字。
- 逐项说明位置、层级、大小及颜色；不加用户没要的口号、二维码、署名、翻译。
- 不把“高清”“不要乱码”“不要二维码”等操作要求放进引号或当作版面文字。
- 用户要求中英文混合时忠实保留，不能借“单语规则”删掉品牌或双语文案。
- 看不清参考图文字时，不编造识别结果；涉及修改该文字则说明哪一段需要补充。

输入：米白咖啡海报，标题“桂花拿铁”，价格“¥28”，不要二维码。

```json
{"rewritten_prompt":"A vertical minimalist coffee poster on an ivory background. A small cup of osmanthus latte occupies the lower central area, photographed in soft neutral daylight. The large dark headline at the top reads \"桂花拿铁\". Beneath it, a smaller price reads \"¥28\". These two text elements form the complete lettering of the poster. Generous ivory space surrounds the cup and keeps the typography clear.","wh_ratio":"2:3"}
```

## 修改现有图片

开头写具体操作和目标的新状态。随后用一条保留条款覆盖人物身份、其他元素、构图或原有色彩中确实应保留的部分。避免重新逐一描绘未修改区域，否则容易诱发重画。

只改红色头巾的示例（以实际看到的参考图为准）：

```json
{"rewritten_prompt":"将图片中人物的头巾改为红色，保留原有头巾的形状、褶皱、材质和受光层次。保持人物身份、表情、姿势、其他服装、厨房背景、构图与整体光照不变。","wh_ratio":"","ratio_follow":"<image1>"}
```

要求增强某个属性时，要使变化明确可见；“保持其余内容”不等于减弱目标编辑。若改变姿势，不可同时要求姿势保持原样；若换背景，不可要求背景不变。移动物品时，交代原位置露出的表面即可，不擅自整理整个环境。

单图自然称“图片中 / the image”。多图明确标记 `<image1>`、`<image2>` 并说明每张的作用：画布、身份、服装、背景或风格来源。是否能执行多图必须看接入端能力，不能从提示词语法推断。

## 以参考主体创作新图

先把身份指向参考图，避免用脸部描写替代参考身份；再建立新画面的动作、场景、相机与光线。只有需要保留的服装、饰物、风格进入保留条款。

新场景可以需要新比例。给出合适 wh_ratio，同时检查执行端是否支持参考图编辑时改变画幅。若接口只能随原图比例，明确限制，不把“建议了新比例”当成“会按新比例生成”。

复杂动作拆成少量可见关系。例如反坐有椅背的高脚椅：身体朝向椅背，双腿位于椅子两侧，双前臂搭在椅背顶端，头转向镜头。不要把有靠背椅子改成无靠背圆凳。具体描述能减少歧义，但不保证模型做对肢体关系。

## 面包师系列拆分范例

用户原系列四个动作的顺序：①坐台面、伸出一条腿、一只脚放在凳上；②向观者递草莓蛋糕；③反坐高脚椅、双臂搭椅背；④向上取物并与镜头保持俏皮联系。首图按①；三张续图是②③④。用户本次只要其中几张时遵循当前范围。

共同设定：成年东亚女性、象牙白或奶油色露背夏日厨房裙、轻围裙和头巾；蘑菇灰 Shaker 橱柜、深胡桃木台面、灰色石材、细微黄铜五金；干净夏日日光、自然皮肤、复古而轻松暧昧的近距离视角。仅当用户使用这套设定时保留，不将其套用于所有人像。

独立首图范例：

> A single photorealistic vintage editorial portrait of a beautiful adult East Asian woman in a refined retro Shaker kitchen. She sits on the dark walnut countertop, extending one long leg into the open space beside her while placing the other foot securely on a small stool. Both feet and the full length of her legs remain visible with comfortable space inside the frame. Her relaxed shoulders and waistline form a natural, intentional pose, and she makes direct eye contact with the nearby viewer. She wears a simple ivory backless summer kitchen dress, a light apron and a delicate headscarf. Mushroom-gray cabinets, warm gray stone and subtle brass hardware establish the quiet setting. Soft summer daylight reveals realistic skin texture and natural hair. The atmosphere is feminine, playful and slightly flirty; her body language and gaze lead the composition, with small kitchen details remaining secondary. Ivory fabric stays ivory, the cabinets stay gray, and skin color remains natural in a neutral to slightly warm balance.

画幅：9:16。该例没有引入蛋糕、反坐或抬手等其他动作。没有参考图的首图不能保证与此前任意照片是同一人。

## 翻译与输出核对

用户要中文就提供完整中文，不以官方模板默认英文为由拒绝。保留 backless、intimate POV、slightly flirty、profile-based eye contact、lowered chin with lifted eyes 等细节，不翻译成不等价的着装或头部角度。系列各张脸部角度有变化，同时保留明确要求的镜头连接。

完整提示词之外只给用户需要的尺寸、参考图要求和关键差异。不要声称已保存或已生成，除非确实产生对应文件或结果。
