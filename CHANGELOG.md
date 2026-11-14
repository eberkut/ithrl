# CHANGELOG

## v0.4.0 (2026-11-14)

### Features

- Inspect recorded operations across compaction ([#69](https://github.com/eberkut/ithrl/pull/69), [`3594d32`](https://github.com/eberkut/ithrl/commit/3594d3212b6dbc92276223f373908529e95a5e9e))
- Prepare native reduction experiments offline ([#66](https://github.com/eberkut/ithrl/pull/66), [`13c9626`](https://github.com/eberkut/ithrl/commit/13c96260335982a3a3ce52ea9c73b327f649efce))
- Qualify passive compaction inspection ([#70](https://github.com/eberkut/ithrl/pull/70), [`3b43a1b`](https://github.com/eberkut/ithrl/commit/3b43a1b8ef0c02bfd0182650460e4efed1c13076))

### Performance Improvements

- Reduce workflow overhead and track performance ([#71](https://github.com/eberkut/ithrl/pull/71), [`ae0c8cd`](https://github.com/eberkut/ithrl/commit/ae0c8cd238eff742ec51076a1054007580c9c0db))

## v0.3.0 (2026-11-10)

### Features

- Add finer final-response reduction ([#52](https://github.com/eberkut/ithrl/pull/52), [`562124a`](https://github.com/eberkut/ithrl/commit/562124ab071788a52e0d50dd538048ee67de464b))
- Define source-bound text-section removal ([#51](https://github.com/eberkut/ithrl/pull/51), [`56c9e6c`](https://github.com/eberkut/ithrl/commit/56c9e6cfb24a83dc32573232b447e574cb0f616f))
- Inspect retained context without provider calls ([#49](https://github.com/eberkut/ithrl/pull/49), [`5604688`](https://github.com/eberkut/ithrl/commit/5604688e269543305fe4b7b253a703dc404c1139))
- **reduce**: Qualify paragraph removal on ordinary sessions ([#58](https://github.com/eberkut/ithrl/pull/58), [`5c6ae26`](https://github.com/eberkut/ithrl/commit/5c6ae26760ea7bd94b406ebf9ba3214e5c4ce64c))

### Refactoring

- Simplify experiment workflows and test fixtures ([#50](https://github.com/eberkut/ithrl/pull/50), [`782bfa2`](https://github.com/eberkut/ithrl/commit/782bfa2692a49a5bb2a4f555801abda23cde6fda))

## v0.2.0 (2026-11-02)

### Bug Fixes

- **codex**: Bound and account for physical provider requests ([#33](https://github.com/eberkut/ithrl/pull/33), [`88529d6`](https://github.com/eberkut/ithrl/commit/88529d6f8b930dbcc546a0011f085c67d0bef1ac))
- **copilot**: Recover catalogue connections and qualify fuzzing ([#37](https://github.com/eberkut/ithrl/pull/37), [`ff6cf63`](https://github.com/eberkut/ithrl/commit/ff6cf6324325f7f6dad8f8c9c80ea58bfe4e249a))
- **copilot**: Restore native session prefixes through the CLI ([#34](https://github.com/eberkut/ithrl/pull/34), [`36845dd`](https://github.com/eberkut/ithrl/commit/36845ddb8239c0a488552297aaa70e6211979e59))
- **fuzz**: Preserve accounting and validate campaign recovery ([#25](https://github.com/eberkut/ithrl/pull/25), [`f1f4793`](https://github.com/eberkut/ithrl/commit/f1f479318fe914f3033667b65381f7101bd3aaea))
- **sampling**: Enforce calibration budgets and report durable progress ([#32](https://github.com/eberkut/ithrl/pull/32), [`bd40577`](https://github.com/eberkut/ithrl/commit/bd40577c734d70b36e59a9e6d3babafa1502bfd3))

### Features

- **extract**: Support literal predicates for native decisions ([#31](https://github.com/eberkut/ithrl/pull/31), [`3b18c2b`](https://github.com/eberkut/ithrl/commit/3b18c2bdd814c4e4c3bf79825a8cab77eb433661))
- **fuzz**: Add mutation and sampled comparison services ([`56ee8b8`](https://github.com/eberkut/ithrl/commit/56ee8b8c5e1370b91db6c9c952eb8ceec19c95ae))
- **fuzz**: Expose inspectable campaigns and saved results ([#24](https://github.com/eberkut/ithrl/pull/24), [`e60e472`](https://github.com/eberkut/ithrl/commit/e60e47230b99e50ff42de7cd8fb89b3769e6d6e2))
- **fuzz**: Persist campaigns and recover interrupted execution ([#22](https://github.com/eberkut/ithrl/pull/22), [`806667f`](https://github.com/eberkut/ithrl/commit/806667f47e8bae1ccbf9b32fadc511ca559675d0))
- **fuzz**: Qualify native Codex text appends ([#28](https://github.com/eberkut/ithrl/pull/28), [`a5b880b`](https://github.com/eberkut/ithrl/commit/a5b880b60ef9834bb57b2c4950e18cd17ff0b275))

### Performance Improvements

- Streamline local workflows and harden result publication ([#36](https://github.com/eberkut/ithrl/pull/36), [`43b9d81`](https://github.com/eberkut/ithrl/commit/43b9d81d0dcff85708da5c914986a68bbe8141db))

## v0.1.0 (2026-10-23)

### Bug Fixes

- **adapters**: Detect saved session formats structurally ([`c8256b9`](https://github.com/eberkut/ithrl/commit/c8256b93ce79fb9c3f025e2b92a970f336497218))
- **reduce**: Validate native candidates before sampled dispatch ([`90d4c0d`](https://github.com/eberkut/ithrl/commit/90d4c0d11a206389d44c6a59991df53c19802514))
- **runner**: Clarify Codex isolation prerequisites ([`4a409c1`](https://github.com/eberkut/ithrl/commit/4a409c1f37caf301a00cfbd20d3a95cf15844eb9))

### Features

- Establish credential-free context reduction core ([`c517409`](https://github.com/eberkut/ithrl/commit/c517409c06e40c92b24a8b990685abd4acc9a9ba))
- **adapters**: Parse Codex and Copilot session evidence ([`3b81c26`](https://github.com/eberkut/ithrl/commit/3b81c2607397d884bcfb1b19dbf995f66ccb0107))
- **calibration**: Add bounded sampled runner calibration ([`f9db180`](https://github.com/eberkut/ithrl/commit/f9db18012565badc2cf2a3a4a6158487d4c5fb70))
- **case**: Preserve immutable native source evidence ([`d108b96`](https://github.com/eberkut/ithrl/commit/d108b96487ccb69f6b36642ae331d36272038722))
- **cli**: Add an installed example and focused public guides ([`1efc2ff`](https://github.com/eberkut/ithrl/commit/1efc2ffb03c734b666b1d1da9b5610ad62970267))
- **cli**: Add guarded native calibration ([`06d2bfb`](https://github.com/eberkut/ithrl/commit/06d2bfb3f4d34f4dc845d9d6877257e4e79f9120))
- **cli**: Integrate sampled reduction and exact resume ([`40be766`](https://github.com/eberkut/ithrl/commit/40be7664ba90ac5341d93fb5a725dbfa0efd48d3))
- **extract**: Support ordinary Codex and Copilot sessions ([`3cd0c10`](https://github.com/eberkut/ithrl/commit/3cd0c1092aeb3669b86fdae07e887d28ba9d685f))
- **reduce**: Add sampled contracts and durable progress ([`650252d`](https://github.com/eberkut/ithrl/commit/650252d76e8d7d72f3f5715ecaf488e752dc18b1))
- **reduce**: Integrate sampled candidate search ([`72958d8`](https://github.com/eberkut/ithrl/commit/72958d8d6ae0307ca2d090a2a10d02606bafdea6))
- **runner**: Add Codex final-response execution ([`ad30a9a`](https://github.com/eberkut/ithrl/commit/ad30a9a17b0e665056eca9b415cbf4e2770a7bf7))
- **runner**: Add controlled Copilot execution ([`c9d1bb2`](https://github.com/eberkut/ithrl/commit/c9d1bb23bfdd98bb022fd0b62ca9b8a81021fe4b))
- **runner**: Add isolated execution runtime ([`17d2224`](https://github.com/eberkut/ithrl/commit/17d22240414cdb32d4464f16e7b2621d99729ca0))
- **runner**: Add shared execution foundation ([`89536e9`](https://github.com/eberkut/ithrl/commit/89536e919730fc0abf680a2928a89761c78339f5))
- **runner**: Validate isolated native calibration ([`b0cd438`](https://github.com/eberkut/ithrl/commit/b0cd438d1fa280f0ddafa39d23653a7ac926da1b))
- **sampling**: Collect bounded fresh-baseline blocks ([`16abf30`](https://github.com/eberkut/ithrl/commit/16abf30b964eae299177d1e1dade6f545ffbda31))
- **sampling**: Support paced execution and safe overload replacement ([`5d70fbd`](https://github.com/eberkut/ithrl/commit/5d70fbd26901a0574883e07fd4055644c8515e80))
- **statistics**: Add fixed-sample candidate classification ([`fb26fca`](https://github.com/eberkut/ithrl/commit/fb26fcaf7c064f49ac7d63b4e2ceb1ab3d6dcdb5))
- **workflow**: Confirm sampled reductions with fresh evidence ([`ee291e4`](https://github.com/eberkut/ithrl/commit/ee291e4d906394452da61640989cef594f62caf7))

### Refactoring

- Simplify execution paths and avoid redundant context copies ([`34ab361`](https://github.com/eberkut/ithrl/commit/34ab36165fed08678a80679b3f1753e6f26f63f8))
