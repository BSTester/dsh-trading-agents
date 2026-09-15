// Markdown 解析纯函数（移植自 plugins/workbench/src/client.js L724-867）断言集。
//
// 断言形状的来源：把 client.js 原函数（parseInline/parseList/parseMarkdown）逐字提取
// 后对本文件每个输入实跑一次，期望值即原函数的真实输出（deepEqual 字面量）；
// markdown-core.js 与原实现归一化后逐行同构，差分校验 zero divergence 后落盘。
// 覆盖面：块级（标题/分隔线/围栏/表格/引用/列表/段落）、输入归一化、行内标记、
// parseList 直测——正则的每个分支与已知边界（未闭合、协议白名单、嵌套缩进）都有用例。
//
// 为什么测 markdown-core.js 而不是 markdown.jsx：node --test 无 JSX 加载器，
// 纯函数单独放在无 React 依赖的 .js 模块里；markdown.jsx re-export 同一组绑定，
// React 渲染部分（renderInline/renderBlocks/Markdown）不进本测试。
import test from "node:test";
import assert from "node:assert/strict";
import { parseInline, parseList, parseMarkdown } from "../src/lib/markdown-core.js";

// ===== 块级：标题 =====

test("标题：# → heading level 1", () => {
  assert.deepEqual(parseMarkdown("# 一级标题"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "一级标题"
        }
      ]
    }
  ]);
});

test("标题：## → heading level 2", () => {
  assert.deepEqual(parseMarkdown("## 二级标题"), [
    {
      "type": "heading",
      "level": 2,
      "children": [
        {
          "type": "text",
          "value": "二级标题"
        }
      ]
    }
  ]);
});

test("标题：### → heading level 3", () => {
  assert.deepEqual(parseMarkdown("### 三级标题"), [
    {
      "type": "heading",
      "level": 3,
      "children": [
        {
          "type": "text",
          "value": "三级标题"
        }
      ]
    }
  ]);
});

test("标题：#### → heading level 4", () => {
  assert.deepEqual(parseMarkdown("#### 四级标题"), [
    {
      "type": "heading",
      "level": 4,
      "children": [
        {
          "type": "text",
          "value": "四级标题"
        }
      ]
    }
  ]);
});

test("标题：##### → heading level 5", () => {
  assert.deepEqual(parseMarkdown("##### 五级标题"), [
    {
      "type": "heading",
      "level": 5,
      "children": [
        {
          "type": "text",
          "value": "五级标题"
        }
      ]
    }
  ]);
});

test("标题：###### → heading level 6", () => {
  assert.deepEqual(parseMarkdown("###### 六级标题"), [
    {
      "type": "heading",
      "level": 6,
      "children": [
        {
          "type": "text",
          "value": "六级标题"
        }
      ]
    }
  ]);
});

test("标题：七个 # 不是标题（正则上限 6）→ 段落", () => {
  assert.deepEqual(parseMarkdown("####### 七个井号"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "####### 七个井号"
        }
      ]
    }
  ]);
});

test("标题：# 后无空格不构成标题 → 段落", () => {
  assert.deepEqual(parseMarkdown("#无空格标题"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "#无空格标题"
        }
      ]
    }
  ]);
});

test("标题：行首空格使 ^(#{1,6}) 不匹配 → 段落", () => {
  assert.deepEqual(parseMarkdown("  ## 缩进标题"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "## 缩进标题"
        }
      ]
    }
  ]);
});

test("标题：标题行内解析行内标记（粗体+代码）", () => {
  assert.deepEqual(parseMarkdown("# 标题含 **粗体** 与 `代码`"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "标题含 "
        },
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "粗体"
            }
          ]
        },
        {
          "type": "text",
          "value": " 与 "
        },
        {
          "type": "code",
          "value": "代码"
        }
      ]
    }
  ]);
});

test("标题：标题文本先 trim 再解析", () => {
  assert.deepEqual(parseMarkdown("#    尾随空格   "), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "尾随空格"
        }
      ]
    }
  ]);
});

test("标题：标题含链接", () => {
  assert.deepEqual(parseMarkdown("# 看 [文档](https://e.com)"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "看 "
        },
        {
          "type": "link",
          "href": "https://e.com",
          "children": [
            {
              "type": "text",
              "value": "文档"
            }
          ]
        }
      ]
    }
  ]);
});

test("标题：段落被标题行截断", () => {
  assert.deepEqual(parseMarkdown("前文段落\n# 标题"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "前文段落"
        }
      ]
    },
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "标题"
        }
      ]
    }
  ]);
});

test("标题：连续两行标题各成一块", () => {
  assert.deepEqual(parseMarkdown("# 甲\n## 乙"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "甲"
        }
      ]
    },
    {
      "type": "heading",
      "level": 2,
      "children": [
        {
          "type": "text",
          "value": "乙"
        }
      ]
    }
  ]);
});

test("标题：标题后紧邻段落互不影响", () => {
  assert.deepEqual(parseMarkdown("# 标题\n正文第一行\n正文第二行"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "标题"
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "正文第一行 正文第二行"
        }
      ]
    }
  ]);
});

test("标题：#/唯一井号行为由原始实现决定（# 后无内容）", () => {
  assert.deepEqual(parseMarkdown("#"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "#"
        }
      ]
    }
  ]);
});

test("标题：## 后仅空格（.* 允许空标题文本 trim 后为空）", () => {
  assert.deepEqual(parseMarkdown("##   "), [
    {
      "type": "heading",
      "level": 2,
      "children": []
    }
  ]);
});

// ===== 块级：分隔线 =====

test("分隔线：三个连字符", () => {
  assert.deepEqual(parseMarkdown("---"), [
    {
      "type": "hr"
    }
  ]);
});

test("分隔线：三个星号", () => {
  assert.deepEqual(parseMarkdown("***"), [
    {
      "type": "hr"
    }
  ]);
});

test("分隔线：三个下划线", () => {
  assert.deepEqual(parseMarkdown("___"), [
    {
      "type": "hr"
    }
  ]);
});

test("分隔线：空格分隔的 - - -", () => {
  assert.deepEqual(parseMarkdown("- - -"), [
    {
      "type": "hr"
    }
  ]);
});

test("分隔线：前后空格仍命中 ^\s*", () => {
  assert.deepEqual(parseMarkdown("  ---  "), [
    {
      "type": "hr"
    }
  ]);
});

// ===== 块级：表格 =====

test("表格：数据行缺少竖线即截断", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| --- | --- |\n| 1 | 2 |\n无管道行\n| 3 | 4 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "无管道行 | 3 | 4 |"
        }
      ]
    }
  ]);
});

// ===== 块级：分隔线 =====

test("分隔线：两个连字符不构成 → 段落", () => {
  assert.deepEqual(parseMarkdown("--"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "--"
        }
      ]
    }
  ]);
});

test("分隔线：混合字符 -*_ 不构成 → 段落", () => {
  assert.deepEqual(parseMarkdown("-*-"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "-*-"
        }
      ]
    }
  ]);
});

test("分隔线：单字符不构成 → 段落", () => {
  assert.deepEqual(parseMarkdown("-"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "-"
        }
      ]
    }
  ]);
});

// ===== 块级：围栏代码块 =====

test("围栏：无语言标注的 ``` 块", () => {
  assert.deepEqual(parseMarkdown("```\nconsole.log(1)\n```"), [
    {
      "type": "code",
      "lang": "",
      "text": "console.log(1)"
    }
  ]);
});

test("围栏：``` 带语言标注 js", () => {
  assert.deepEqual(parseMarkdown("```js\nconst a = 1;\n```"), [
    {
      "type": "code",
      "lang": "js",
      "text": "const a = 1;"
    }
  ]);
});

test("围栏：~~~ 成对围栏", () => {
  assert.deepEqual(parseMarkdown("~~~\nplain text\n~~~"), [
    {
      "type": "code",
      "lang": "",
      "text": "plain text"
    }
  ]);
});

test("围栏：~~~ 带语言标注 python", () => {
  assert.deepEqual(parseMarkdown("~~~python\nprint(1)\n~~~"), [
    {
      "type": "code",
      "lang": "python",
      "text": "print(1)"
    }
  ]);
});

test("围栏：未闭合时余下行全部并入正文", () => {
  assert.deepEqual(parseMarkdown("```\n未闭合围栏\n还有一行"), [
    {
      "type": "code",
      "lang": "",
      "text": "未闭合围栏\n还有一行"
    }
  ]);
});

test("围栏：空代码块（开合相邻）", () => {
  assert.deepEqual(parseMarkdown("```\n```"), [
    {
      "type": "code",
      "lang": "",
      "text": ""
    }
  ]);
});

test("围栏：缩进的围栏与缩进收尾", () => {
  assert.deepEqual(parseMarkdown("  ```\n  缩进内容\n  ```"), [
    {
      "type": "code",
      "lang": "",
      "text": "  缩进内容"
    }
  ]);
});

test("围栏：代码内容不解析块级与行内标记", () => {
  assert.deepEqual(parseMarkdown("```\n# 不是标题 **不是粗体** | 表 |\n```"), [
    {
      "type": "code",
      "lang": "",
      "text": "# 不是标题 **不是粗体** | 表 |"
    }
  ]);
});

test("围栏：空行保留在代码正文里", () => {
  assert.deepEqual(parseMarkdown("```\n\n空行之上\n\n空行之下\n\n```"), [
    {
      "type": "code",
      "lang": "",
      "text": "\n空行之上\n\n空行之下\n"
    }
  ]);
});

test("围栏：语言标注取 \S* 首段", () => {
  assert.deepEqual(parseMarkdown("```js extra\n内容\n```"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "```js extra 内容"
        }
      ]
    },
    {
      "type": "code",
      "lang": "",
      "text": ""
    }
  ]);
});

test("围栏：~~~ 块内含 ``` 行不被截断", () => {
  assert.deepEqual(parseMarkdown("~~~\n``` 围栏内的反引号行\n~~~"), [
    {
      "type": "code",
      "lang": "",
      "text": "``` 围栏内的反引号行"
    }
  ]);
});

test("围栏：收尾围栏缩进后仍命中 ^\s*```", () => {
  assert.deepEqual(parseMarkdown("```\n内容\n  ```"), [
    {
      "type": "code",
      "lang": "",
      "text": "内容"
    }
  ]);
});

test("围栏：内容含 --- 不受分隔线影响", () => {
  assert.deepEqual(parseMarkdown("```\n---\n```"), [
    {
      "type": "code",
      "lang": "",
      "text": "---"
    }
  ]);
});

test("围栏：空围栏后接段落", () => {
  assert.deepEqual(parseMarkdown("```\n```\n后续段落"), [
    {
      "type": "code",
      "lang": "",
      "text": ""
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "后续段落"
        }
      ]
    }
  ]);
});

// ===== 块级：表格 =====

test("表格：标准两列三行", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| --- | --- |\n| 1 | 2 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：分隔行冒号对齐（:- -:）按普通单元格忽略", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| :- | -: |\n| x | y |"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "| A | B | | :- | -: | | x | y |"
        }
      ]
    }
  ]);
});

test("表格：无首尾管道的分隔行", () => {
  assert.deepEqual(parseMarkdown("A | B\n--- | ---\n1 | 2"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：单列表", () => {
  assert.deepEqual(parseMarkdown("| 单列 |\n| --- |\n| 值 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "单列"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "值"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：第二行非分隔行 → 整体按段落走", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| 只有格 |\n| 1 | 2 |"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "| A | B | | 只有格 | | 1 | 2 |"
        }
      ]
    }
  ]);
});

test("表格：两行数据行", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ],
        [
          [
            {
              "type": "text",
              "value": "3"
            }
          ],
          [
            {
              "type": "text",
              "value": "4"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：只有表头与分隔行（无数据）", () => {
  assert.deepEqual(parseMarkdown("| A |\n| --- |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ]
      ],
      "rows": []
    }
  ]);
});

test("表格：表头与单元格内的行内标记", () => {
  assert.deepEqual(parseMarkdown("| **表头** | `代码` |\n| --- | --- |\n| [链接](https://e.com) | 普通 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "strong",
            "children": [
              {
                "type": "text",
                "value": "表头"
              }
            ]
          }
        ],
        [
          {
            "type": "code",
            "value": "代码"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "link",
              "href": "https://e.com",
              "children": [
                {
                  "type": "text",
                  "value": "链接"
                }
              ]
            }
          ],
          [
            {
              "type": "text",
              "value": "普通"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：分隔行行首缩进仍命中 TABLE_DIVIDER", () => {
  assert.deepEqual(parseMarkdown("A | B\n  --- | ---\n  1 | 2"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：单元格两侧空格被 trim", () => {
  assert.deepEqual(parseMarkdown("|  左  |  右  |\n| --- | --- |\n|  值1  |  值2  |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "左"
          }
        ],
        [
          {
            "type": "text",
            "value": "右"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "值1"
            }
          ],
          [
            {
              "type": "text",
              "value": "值2"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：数据行含空单元格", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| --- | --- |\n|  | 2 |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：数据行空行截断后接段落", () => {
  assert.deepEqual(parseMarkdown("| A |\n| --- |\n| 1 |\n\n后文"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ]
        ]
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "后文"
        }
      ]
    }
  ]);
});

test("表格：单元格内竖线按字面切分（不支持转义）", () => {
  assert.deepEqual(parseMarkdown("| A | B |\n| --- | --- |\n| a\\|b | c |"), [
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "a\\"
            }
          ],
          [
            {
              "type": "text",
              "value": "b"
            }
          ],
          [
            {
              "type": "text",
              "value": "c"
            }
          ]
        ]
      ]
    }
  ]);
});

test("表格：分隔行连字符不足两个不命中 → 段落", () => {
  assert.deepEqual(parseMarkdown("| A |\n| - |\n| 1 |"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "| A | | - | | 1 |"
        }
      ]
    }
  ]);
});

// ===== 块级：引用 =====

test("引用：单行", () => {
  assert.deepEqual(parseMarkdown("> 引用一行"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用一行"
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：多行合并再解析", () => {
  assert.deepEqual(parseMarkdown("> 引用多行\n> 第二行"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用多行 第二行"
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：> 后无空格也剥掉一个 >", () => {
  assert.deepEqual(parseMarkdown(">无空格引用\n>继续"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "无空格引用 继续"
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：引用内行内标记", () => {
  assert.deepEqual(parseMarkdown("> 引用内 **粗体** 与 `代码`"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用内 "
            },
            {
              "type": "strong",
              "children": [
                {
                  "type": "text",
                  "value": "粗体"
                }
              ]
            },
            {
              "type": "text",
              "value": " 与 "
            },
            {
              "type": "code",
              "value": "代码"
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：引用内块级结构（标题+段落）", () => {
  assert.deepEqual(parseMarkdown("> # 引用内标题\n> 引用内段落"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "heading",
          "level": 1,
          "children": [
            {
              "type": "text",
              "value": "引用内标题"
            }
          ]
        },
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用内段落"
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：嵌套引用", () => {
  assert.deepEqual(parseMarkdown("> 外层\n> > 内层"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "外层"
            }
          ]
        },
        {
          "type": "quote",
          "children": [
            {
              "type": "paragraph",
              "children": [
                {
                  "type": "text",
                  "value": "内层"
                }
              ]
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：引用结束后接段落", () => {
  assert.deepEqual(parseMarkdown("> 引用\n普通段落"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用"
            }
          ]
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "普通段落"
        }
      ]
    }
  ]);
});

test("引用：引用内列表", () => {
  assert.deepEqual(parseMarkdown("> - 引用内列表项"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "list",
          "ordered": false,
          "items": [
            {
              "children": [
                {
                  "type": "text",
                  "value": "引用内列表项"
                }
              ]
            }
          ]
        }
      ]
    }
  ]);
});

test("引用：引用内空行截断（空行不带 >）", () => {
  assert.deepEqual(parseMarkdown("> 第一段\n\n普通段落"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "第一段"
            }
          ]
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "普通段落"
        }
      ]
    }
  ]);
});

test("引用：引用内代码块", () => {
  assert.deepEqual(parseMarkdown("> ```\n> 代码行\n> ```"), [
    {
      "type": "quote",
      "children": [
        {
          "type": "code",
          "lang": "",
          "text": "代码行"
        }
      ]
    }
  ]);
});

// ===== 块级：列表 =====

test("列表：无序 - 两项", () => {
  assert.deepEqual(parseMarkdown("- 甲\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：无序 * 两项", () => {
  assert.deepEqual(parseMarkdown("* 甲\n* 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：无序 + 两项", () => {
  assert.deepEqual(parseMarkdown("+ 甲\n+ 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：有序 1. 两项", () => {
  assert.deepEqual(parseMarkdown("1. 甲\n2. 乙"), [
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：有序 1) 两项", () => {
  assert.deepEqual(parseMarkdown("1) 甲\n2) 乙"), [
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：二级嵌套挂到父项 sub", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  - 甲一\n  - 甲二\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "甲一"
                  }
                ]
              },
              {
                "children": [
                  {
                    "type": "text",
                    "value": "甲二"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：三级嵌套", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  - 嵌套\n    - 更深\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "嵌套"
                  }
                ],
                "sub": {
                  "type": "list",
                  "ordered": false,
                  "items": [
                    {
                      "children": [
                        {
                          "type": "text",
                          "value": "更深"
                        }
                      ]
                    }
                  ]
                }
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：有序嵌套有序", () => {
  assert.deepEqual(parseMarkdown("1. 甲\n   1. 嵌套有序\n2. 乙"), [
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": true,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "嵌套有序"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：续行并入项文本（空格连接）", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  续行内容\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲 续行内容"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：多行续行依次并入", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  续行一\n  续行二\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲 续行一 续行二"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：空行截断成两个列表", () => {
  assert.deepEqual(parseMarkdown("- 甲\n\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：- 后无空格不是列表项 → 段落", () => {
  assert.deepEqual(parseMarkdown("-甲 无空格"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "-甲 无空格"
        }
      ]
    }
  ]);
});

test("列表：两位数序号", () => {
  assert.deepEqual(parseMarkdown("10. 第十条\n11. 第十一条"), [
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "第十条"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "第十一条"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：项文本行内粗体", () => {
  assert.deepEqual(parseMarkdown("- **粗体项**"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "strong",
              "children": [
                {
                  "type": "text",
                  "value": "粗体项"
                }
              ]
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：项文本链接", () => {
  assert.deepEqual(parseMarkdown("- [链接项](https://e.com)"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "link",
              "href": "https://e.com",
              "children": [
                {
                  "type": "text",
                  "value": "链接项"
                }
              ]
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：项文本行内代码", () => {
  assert.deepEqual(parseMarkdown("- `代码项`"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "code",
              "value": "代码项"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：先无序后有序（中间空行）", () => {
  assert.deepEqual(parseMarkdown("- 无序\n\n1. 有序"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "无序"
            }
          ]
        }
      ]
    },
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "有序"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：四空格缩进嵌套", () => {
  assert.deepEqual(parseMarkdown("- 甲\n    - 深缩进\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "深缩进"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：整体缩进的顶层列表", () => {
  assert.deepEqual(parseMarkdown("  - 缩进列表\n  - 第二项"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "缩进列表"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "第二项"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：子列表结束后回到父层", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  - 子\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "子"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：父无序子有序（嵌套 ordered 判定）", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  1. 内一号\n  2. 内二号"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": true,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "内一号"
                  }
                ]
              },
              {
                "children": [
                  {
                    "type": "text",
                    "value": "内二号"
                  }
                ]
              }
            ]
          }
        }
      ]
    }
  ]);
});

test("列表：项文本含竖线不成表", () => {
  assert.deepEqual(parseMarkdown("- 项目含 | 竖线\n- 第二项"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "项目含 | 竖线"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "第二项"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：ordered 判定只看首项标记", () => {
  assert.deepEqual(parseMarkdown("1. 首项有序\n- 次项无序（缩进不同即断）"), [
    {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "首项有序"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "次项无序（缩进不同即断）"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：项文本以 # 开头仍按列表项处理", () => {
  assert.deepEqual(parseMarkdown("- # 井号开头的项"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "# 井号开头的项"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：续行是普通文字不含列表标记", () => {
  assert.deepEqual(parseMarkdown("- 甲\n  这是续行不是新项"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲 这是续行不是新项"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：嵌套项文本行内标记", () => {
  assert.deepEqual(parseMarkdown("- 父 **粗**\n  - 子 `码`"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "父 "
            },
            {
              "type": "strong",
              "children": [
                {
                  "type": "text",
                  "value": "粗"
                }
              ]
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "子 "
                  },
                  {
                    "type": "code",
                    "value": "码"
                  }
                ]
              }
            ]
          }
        }
      ]
    }
  ]);
});

test("列表：列表后接段落", () => {
  assert.deepEqual(parseMarkdown("- 甲\n\n后继段落"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "后继段落"
        }
      ]
    }
  ]);
});

test("列表：列表项后直接接段落（无空行，续行缩进不足即断）", () => {
  assert.deepEqual(parseMarkdown("- 甲\n后继段落"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "后继段落"
        }
      ]
    }
  ]);
});

test("列表：* 项与 - 项同级混用（标记不同但同为无序）", () => {
  assert.deepEqual(parseMarkdown("* 甲\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：缩进 1 空格的子列表（>0 即嵌套）", () => {
  assert.deepEqual(parseMarkdown("- 甲\n - 一格缩进子项\n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "一格缩进子项"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：空项文本（标记后直接换行不行，需空格）", () => {
  assert.deepEqual(parseMarkdown("- \n- 乙"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": []
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    }
  ]);
});

test("列表：中文序号不构成有序列表", () => {
  assert.deepEqual(parseMarkdown("一、甲\n二、乙"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "一、甲 二、乙"
        }
      ]
    }
  ]);
});

// ===== 块级：段落 =====

test("段落：单行", () => {
  assert.deepEqual(parseMarkdown("单行段落"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "单行段落"
        }
      ]
    }
  ]);
});

test("段落：相邻行软换行合并（空格连接）", () => {
  assert.deepEqual(parseMarkdown("第一行\n第二行"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "第一行 第二行"
        }
      ]
    }
  ]);
});

test("段落：空行分段", () => {
  assert.deepEqual(parseMarkdown("段落一\n\n段落二"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落一"
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落二"
        }
      ]
    }
  ]);
});

test("段落：段内行内标记全谱", () => {
  assert.deepEqual(parseMarkdown("含 **粗**、*斜*、`码`、[链](https://e.com)"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "含 "
        },
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "粗"
            }
          ]
        },
        {
          "type": "text",
          "value": "、"
        },
        {
          "type": "em",
          "children": [
            {
              "type": "text",
              "value": "斜"
            }
          ]
        },
        {
          "type": "text",
          "value": "、"
        },
        {
          "type": "code",
          "value": "码"
        },
        {
          "type": "text",
          "value": "、"
        },
        {
          "type": "link",
          "href": "https://e.com",
          "children": [
            {
              "type": "text",
              "value": "链"
            }
          ]
        }
      ]
    }
  ]);
});

test("段落：段落被列表行截断", () => {
  assert.deepEqual(parseMarkdown("段落\n- 列表项"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落"
        }
      ]
    },
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "列表项"
            }
          ]
        }
      ]
    }
  ]);
});

test("段落：段落被引用行截断", () => {
  assert.deepEqual(parseMarkdown("段落\n> 引用"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落"
        }
      ]
    },
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用"
            }
          ]
        }
      ]
    }
  ]);
});

test("段落：段落被围栏行截断", () => {
  assert.deepEqual(parseMarkdown("段落\n```\n围栏内容"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落"
        }
      ]
    },
    {
      "type": "code",
      "lang": "",
      "text": "围栏内容"
    }
  ]);
});

test("段落：段落被表格截断（前瞻分隔行）", () => {
  assert.deepEqual(parseMarkdown("段落\n| A | B |\n| --- | --- |"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落"
        }
      ]
    },
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "A"
          }
        ],
        [
          {
            "type": "text",
            "value": "B"
          }
        ]
      ],
      "rows": []
    }
  ]);
});

test("段落：行尾空格 trim", () => {
  assert.deepEqual(parseMarkdown("行尾空格   \n第二行"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "行尾空格 第二行"
        }
      ]
    }
  ]);
});

test("段落：多行合并后行内解析一次完成", () => {
  assert.deepEqual(parseMarkdown("**跨**\n**行**粗体各自成对"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "跨"
            }
          ]
        },
        {
          "type": "text",
          "value": " "
        },
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "行"
            }
          ]
        },
        {
          "type": "text",
          "value": "粗体各自成对"
        }
      ]
    }
  ]);
});

test("段落：数字单行也是段落", () => {
  assert.deepEqual(parseMarkdown("42"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "42"
        }
      ]
    }
  ]);
});

// ===== 输入归一化 =====

test("归一化：CRLF 统一为 LF", () => {
  assert.deepEqual(parseMarkdown("第一行\r\n第二行\r\n\r\n## 标题\r\n"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "第一行 第二行"
        }
      ]
    },
    {
      "type": "heading",
      "level": 2,
      "children": [
        {
          "type": "text",
          "value": "标题"
        }
      ]
    }
  ]);
});

test("归一化：孤立 CR 也归一", () => {
  assert.deepEqual(parseMarkdown("第一行\r第二行"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "第一行 第二行"
        }
      ]
    }
  ]);
});

test("归一化：全空白输入 → 空块", () => {
  assert.deepEqual(parseMarkdown("\n\n   \n\t\n"), []);
});

test("归一化：空字符串 → 空块", () => {
  assert.deepEqual(parseMarkdown(""), []);
});

test("归一化：null → 空块", () => {
  assert.deepEqual(parseMarkdown(null), []);
});

test("归一化：undefined → 空块", () => {
  assert.deepEqual(parseMarkdown(undefined), []);
});

test("归一化：非字符串输入转字符串", () => {
  assert.deepEqual(parseMarkdown(42), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "42"
        }
      ]
    }
  ]);
});

test("归一化：收尾换行不产生尾块", () => {
  assert.deepEqual(parseMarkdown("段落\n"), [
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "段落"
        }
      ]
    }
  ]);
});

test("归一化：混合换行符", () => {
  assert.deepEqual(parseMarkdown("- 甲\r\n- 乙\n> 引用"), [
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用"
            }
          ]
        }
      ]
    }
  ]);
});

// ===== 复合文档 =====

test("复合：标题+段落+列表+引用+表格+围栏完整文档", () => {
  assert.deepEqual(parseMarkdown("# 报告\n\n导语段落。\n\n## 要点\n\n- 甲\n- 乙\n\n> 引用说明\n\n| 指 | 值 |\n| --- | --- |\n| IC | 0.05 |\n\n```py\ncode()\n```\n"), [
    {
      "type": "heading",
      "level": 1,
      "children": [
        {
          "type": "text",
          "value": "报告"
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "导语段落。"
        }
      ]
    },
    {
      "type": "heading",
      "level": 2,
      "children": [
        {
          "type": "text",
          "value": "要点"
        }
      ]
    },
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    {
      "type": "quote",
      "children": [
        {
          "type": "paragraph",
          "children": [
            {
              "type": "text",
              "value": "引用说明"
            }
          ]
        }
      ]
    },
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "指"
          }
        ],
        [
          {
            "type": "text",
            "value": "值"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "IC"
            }
          ],
          [
            {
              "type": "text",
              "value": "0.05"
            }
          ]
        ]
      ]
    },
    {
      "type": "code",
      "lang": "py",
      "text": "code()"
    }
  ]);
});

test("复合：研报式长文档（多级标题与嵌套列表）", () => {
  assert.deepEqual(parseMarkdown("## 结论\n\n正文 **加粗**。\n\n- 结论一\n  - 依据 1\n  - 依据 2\n- 结论二\n\n---\n\n### 数据\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"), [
    {
      "type": "heading",
      "level": 2,
      "children": [
        {
          "type": "text",
          "value": "结论"
        }
      ]
    },
    {
      "type": "paragraph",
      "children": [
        {
          "type": "text",
          "value": "正文 "
        },
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "加粗"
            }
          ]
        },
        {
          "type": "text",
          "value": "。"
        }
      ]
    },
    {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "结论一"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "依据 1"
                  }
                ]
              },
              {
                "children": [
                  {
                    "type": "text",
                    "value": "依据 2"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "结论二"
            }
          ]
        }
      ]
    },
    {
      "type": "hr"
    },
    {
      "type": "heading",
      "level": 3,
      "children": [
        {
          "type": "text",
          "value": "数据"
        }
      ]
    },
    {
      "type": "table",
      "header": [
        [
          {
            "type": "text",
            "value": "a"
          }
        ],
        [
          {
            "type": "text",
            "value": "b"
          }
        ]
      ],
      "rows": [
        [
          [
            {
              "type": "text",
              "value": "1"
            }
          ],
          [
            {
              "type": "text",
              "value": "2"
            }
          ]
        ]
      ]
    }
  ]);
});

// ===== 行内：parseInline =====

test("行内：纯文本", () => {
  assert.deepEqual(parseInline("纯文本内容"), [
    {
      "type": "text",
      "value": "纯文本内容"
    }
  ]);
});

test("行内：空字符串", () => {
  assert.deepEqual(parseInline(""), []);
});

test("行内：null 归一为空串", () => {
  assert.deepEqual(parseInline(null), []);
});

test("行内：undefined 归一为空串", () => {
  assert.deepEqual(parseInline(undefined), []);
});

test("行内：数字转字符串", () => {
  assert.deepEqual(parseInline(42), [
    {
      "type": "text",
      "value": "42"
    }
  ]);
});

test("行内：行内代码", () => {
  assert.deepEqual(parseInline("`代码`"), [
    {
      "type": "code",
      "value": "代码"
    }
  ]);
});

test("行内：未闭合反引号按字面", () => {
  assert.deepEqual(parseInline("`未闭合"), [
    {
      "type": "text",
      "value": "`未闭合"
    }
  ]);
});

test("行内：两段代码夹文本", () => {
  assert.deepEqual(parseInline("`甲`中`乙`"), [
    {
      "type": "code",
      "value": "甲"
    },
    {
      "type": "text",
      "value": "中"
    },
    {
      "type": "code",
      "value": "乙"
    }
  ]);
});

test("行内：空代码对（`` 之间无内容不匹配）", () => {
  assert.deepEqual(parseInline("``"), [
    {
      "type": "text",
      "value": "``"
    }
  ]);
});

test("行内：代码优先于粗体（`**a**` 不成粗体）", () => {
  assert.deepEqual(parseInline("`**不是粗体**`"), [
    {
      "type": "code",
      "value": "**不是粗体**"
    }
  ]);
});

test("行内：粗体内代码也照常解析", () => {
  assert.deepEqual(parseInline("**`代码`**"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "code",
          "value": "代码"
        }
      ]
    }
  ]);
});

test("行内：** 粗体", () => {
  assert.deepEqual(parseInline("**粗体**"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "粗体"
        }
      ]
    }
  ]);
});

test("行内：__ 粗体", () => {
  assert.deepEqual(parseInline("__粗体__"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "粗体"
        }
      ]
    }
  ]);
});

test("行内：* 斜体", () => {
  assert.deepEqual(parseInline("*斜体*"), [
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "斜体"
        }
      ]
    }
  ]);
});

test("行内：_ 斜体", () => {
  assert.deepEqual(parseInline("_斜体_"), [
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "斜体"
        }
      ]
    }
  ]);
});

test("行内：未闭合 ** 按字面", () => {
  assert.deepEqual(parseInline("**未闭合"), [
    {
      "type": "text",
      "value": "**未闭合"
    }
  ]);
});

test("行内：未闭合 __ 按字面", () => {
  assert.deepEqual(parseInline("__未闭合"), [
    {
      "type": "text",
      "value": "__未闭合"
    }
  ]);
});

test("行内：未闭合 * 按字面", () => {
  assert.deepEqual(parseInline("*未闭合"), [
    {
      "type": "text",
      "value": "*未闭合"
    }
  ]);
});

test("行内：未闭合 _ 按字面", () => {
  assert.deepEqual(parseInline("_未闭合"), [
    {
      "type": "text",
      "value": "_未闭合"
    }
  ]);
});

test("行内：三个星号不构成强调", () => {
  assert.deepEqual(parseInline("***"), [
    {
      "type": "text",
      "value": "***"
    }
  ]);
});

test("行内：snake_case 中 _case_ 会成斜体（原实现如此）", () => {
  assert.deepEqual(parseInline("snake_case_name"), [
    {
      "type": "text",
      "value": "snake"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "case"
        }
      ]
    },
    {
      "type": "text",
      "value": "name"
    }
  ]);
});

test("行内：粗体嵌斜体", () => {
  assert.deepEqual(parseInline("**外 *内* 外**"), [
    {
      "type": "text",
      "value": "*"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "外 "
        }
      ]
    },
    {
      "type": "text",
      "value": "内"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": " 外"
        }
      ]
    },
    {
      "type": "text",
      "value": "*"
    }
  ]);
});

test("行内：斜体嵌粗体", () => {
  assert.deepEqual(parseInline("*外 **内** 外*"), [
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "外 "
        }
      ]
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "内"
        }
      ]
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": " 外"
        }
      ]
    }
  ]);
});

test("行内：链接基础形", () => {
  assert.deepEqual(parseInline("[文本](https://e.com)"), [
    {
      "type": "link",
      "href": "https://e.com",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：空链接文本", () => {
  assert.deepEqual(parseInline("[](https://e.com)"), [
    {
      "type": "link",
      "href": "https://e.com",
      "children": []
    }
  ]);
});

test("行内：空 URL 不构成链接", () => {
  assert.deepEqual(parseInline("[文本]()"), [
    {
      "type": "text",
      "value": "[文本]()"
    }
  ]);
});

test("行内：javascript: 协议被拒按字面", () => {
  assert.deepEqual(parseInline("[文本](javascript:alert(1))"), [
    {
      "type": "text",
      "value": "[文本](javascript:alert(1)"
    },
    {
      "type": "text",
      "value": ")"
    }
  ]);
});

test("行内：data: 协议被拒按字面", () => {
  assert.deepEqual(parseInline("[文本](data:text/html,hi)"), [
    {
      "type": "text",
      "value": "[文本](data:text/html,hi)"
    }
  ]);
});

test("行内：相对路径 / 允许", () => {
  assert.deepEqual(parseInline("[文本](/relative/path)"), [
    {
      "type": "link",
      "href": "/relative/path",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：锚点 # 允许", () => {
  assert.deepEqual(parseInline("[文本](#anchor)"), [
    {
      "type": "link",
      "href": "#anchor",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：mailto: 允许", () => {
  assert.deepEqual(parseInline("[文本](mailto:a@b.c)"), [
    {
      "type": "link",
      "href": "mailto:a@b.c",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：协议判定大小写不敏感", () => {
  assert.deepEqual(parseInline("[文本](HTTPS://E.COM/A)"), [
    {
      "type": "link",
      "href": "HTTPS://E.COM/A",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：URL 带查询串", () => {
  assert.deepEqual(parseInline("[文本](https://e.com/a?b=1&c=2)"), [
    {
      "type": "link",
      "href": "https://e.com/a?b=1&c=2",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：URL 带下划线", () => {
  assert.deepEqual(parseInline("[文本](https://e.com/a_b)"), [
    {
      "type": "link",
      "href": "https://e.com/a_b",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

test("行内：URL 含空格不构成链接", () => {
  assert.deepEqual(parseInline("[文本](https://e.com a)"), [
    {
      "type": "text",
      "value": "[文本](https://e.com a)"
    }
  ]);
});

test("行内：链接文本含粗体", () => {
  assert.deepEqual(parseInline("[**粗**](https://e.com)"), [
    {
      "type": "link",
      "href": "https://e.com",
      "children": [
        {
          "type": "strong",
          "children": [
            {
              "type": "text",
              "value": "粗"
            }
          ]
        }
      ]
    }
  ]);
});

test("行内：链接文本含代码", () => {
  assert.deepEqual(parseInline("[`码`](https://e.com)"), [
    {
      "type": "link",
      "href": "https://e.com",
      "children": [
        {
          "type": "code",
          "value": "码"
        }
      ]
    }
  ]);
});

test("行内：链接前后缀文本", () => {
  assert.deepEqual(parseInline("前 [链](https://e.com) 后"), [
    {
      "type": "text",
      "value": "前 "
    },
    {
      "type": "link",
      "href": "https://e.com",
      "children": [
        {
          "type": "text",
          "value": "链"
        }
      ]
    },
    {
      "type": "text",
      "value": " 后"
    }
  ]);
});

test("行内：裸 URL 不自动成链接", () => {
  assert.deepEqual(parseInline("看 https://e.com 原文"), [
    {
      "type": "text",
      "value": "看 https://e.com 原文"
    }
  ]);
});

test("行内：混合强调与链接", () => {
  assert.deepEqual(parseInline("**粗** 与 *斜* 与 [链](https://e.com)"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "粗"
        }
      ]
    },
    {
      "type": "text",
      "value": " 与 "
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "斜"
        }
      ]
    },
    {
      "type": "text",
      "value": " 与 "
    },
    {
      "type": "link",
      "href": "https://e.com",
      "children": [
        {
          "type": "text",
          "value": "链"
        }
      ]
    }
  ]);
});

test("行内：粗体后紧跟普通星号", () => {
  assert.deepEqual(parseInline("**粗** *"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "粗"
        }
      ]
    },
    {
      "type": "text",
      "value": " *"
    }
  ]);
});

test("行内：下划线粗体嵌斜体", () => {
  assert.deepEqual(parseInline("__外 _内_ 外__"), [
    {
      "type": "text",
      "value": "_"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "外 "
        }
      ]
    },
    {
      "type": "text",
      "value": "内"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": " 外"
        }
      ]
    },
    {
      "type": "text",
      "value": "_"
    }
  ]);
});

test("行内：星号夹字不成对", () => {
  assert.deepEqual(parseInline("a*b*c"), [
    {
      "type": "text",
      "value": "a"
    },
    {
      "type": "em",
      "children": [
        {
          "type": "text",
          "value": "b"
        }
      ]
    },
    {
      "type": "text",
      "value": "c"
    }
  ]);
});

test("行内：连续两组粗体", () => {
  assert.deepEqual(parseInline("**甲** **乙**"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "甲"
        }
      ]
    },
    {
      "type": "text",
      "value": " "
    },
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "乙"
        }
      ]
    }
  ]);
});

test("行内：粗体紧邻文本", () => {
  assert.deepEqual(parseInline("前缀**粗**后缀"), [
    {
      "type": "text",
      "value": "前缀"
    },
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "粗"
        }
      ]
    },
    {
      "type": "text",
      "value": "后缀"
    }
  ]);
});

test("行内：反引号内竖线与方括号按字面", () => {
  assert.deepEqual(parseInline("`a|b [c](d)`"), [
    {
      "type": "code",
      "value": "a|b [c](d)"
    }
  ]);
});

test("行内：换行符阻止 * 强调（[^*\n]+）", () => {
  assert.deepEqual(parseInline("*第一行\n第二行*"), [
    {
      "type": "text",
      "value": "*第一行\n第二行*"
    }
  ]);
});

test("行内：换行符阻止 _ 强调", () => {
  assert.deepEqual(parseInline("_第一行\n第二行_"), [
    {
      "type": "text",
      "value": "_第一行\n第二行_"
    }
  ]);
});

test("行内：换行不影响 ** 粗体（[^*]+ 允许换行）", () => {
  assert.deepEqual(parseInline("**第一行\n第二行**"), [
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "第一行\n第二行"
        }
      ]
    }
  ]);
});

test("行内：链接文本中的右中括号截断匹配", () => {
  assert.deepEqual(parseInline("[a]b](https://e.com)"), [
    {
      "type": "text",
      "value": "[a]b](https://e.com)"
    }
  ]);
});

test("行内：单个反引号成对出现在词中", () => {
  assert.deepEqual(parseInline("a`b`c"), [
    {
      "type": "text",
      "value": "a"
    },
    {
      "type": "code",
      "value": "b"
    },
    {
      "type": "text",
      "value": "c"
    }
  ]);
});

test("行内：转义序列原样保留（不支持反斜杠转义）", () => {
  assert.deepEqual(parseInline("\\**不转义**"), [
    {
      "type": "text",
      "value": "\\"
    },
    {
      "type": "strong",
      "children": [
        {
          "type": "text",
          "value": "不转义"
        }
      ]
    }
  ]);
});

test("行内：全角括号不构成链接", () => {
  assert.deepEqual(parseInline("【文本】（https://e.com）"), [
    {
      "type": "text",
      "value": "【文本】（https://e.com）"
    }
  ]);
});

test("行内：协议相对 // 不在白名单按字面", () => {
  assert.deepEqual(parseInline("[文本](//cdn.e.com/x)"), [
    {
      "type": "link",
      "href": "//cdn.e.com/x",
      "children": [
        {
          "type": "text",
          "value": "文本"
        }
      ]
    }
  ]);
});

// ===== 列表：parseList =====

test("parseList：两项基础", () => {
  assert.deepEqual(parseList(["- 甲","- 乙"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    "next": 2
  });
});

test("parseList：ordered 判定取首项标记", () => {
  assert.deepEqual(parseList(["1. 甲","2. 乙"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    "next": 2
  });
});

test("parseList：缩进不符即断", () => {
  assert.deepEqual(parseList(["- 甲","  - 子","- 乙"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "子"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    "next": 3
  });
});

test("parseList：续行并入", () => {
  assert.deepEqual(parseList(["- 甲","  续行","- 乙"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲 续行"
            }
          ]
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    "next": 3
  });
});

test("parseList：子列表挂 sub 且返回 next 越过子层", () => {
  assert.deepEqual(parseList(["- 甲","  - 子一","  - 子二","- 乙"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ],
          "sub": {
            "type": "list",
            "ordered": false,
            "items": [
              {
                "children": [
                  {
                    "type": "text",
                    "value": "子一"
                  }
                ]
              },
              {
                "children": [
                  {
                    "type": "text",
                    "value": "子二"
                  }
                ]
              }
            ]
          }
        },
        {
          "children": [
            {
              "type": "text",
              "value": "乙"
            }
          ]
        }
      ]
    },
    "next": 4
  });
});

test("parseList：+ 标记", () => {
  assert.deepEqual(parseList(["+ 甲"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    "next": 1
  });
});

test("parseList：) 序号", () => {
  assert.deepEqual(parseList(["1) 甲"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": true,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    "next": 1
  });
});

test("parseList：起点非列表行（防御：调用方保证，此处看行为）", () => {
  assert.deepEqual(parseList(["文字","- 甲"], 1, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "text",
              "value": "甲"
            }
          ]
        }
      ]
    },
    "next": 2
  });
});

test("parseList：项内行内标记原样留给 parseInline", () => {
  assert.deepEqual(parseList(["- **粗**"], 0, 0), {
    "list": {
      "type": "list",
      "ordered": false,
      "items": [
        {
          "children": [
            {
              "type": "strong",
              "children": [
                {
                  "type": "text",
                  "value": "粗"
                }
              ]
            }
          ]
        }
      ]
    },
    "next": 1
  });
});
