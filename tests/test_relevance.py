from src.collector.relevance import decide_text_relevance


def test_crowdsourcing_theme_drops_generic_automation_forum_noise():
    text = (
        "Why is the motor not working properly? https://forumelectrical.com/category/machines/electric-motors/ "
        "Follow Us On: LinkedIn admin/dashboard/ Twitter x.com/ForumElectrical"
    )

    decision = decide_text_relevance(
        text,
        include_themes=["众包任务"],
        min_keyword_hits=1,
    )

    assert decision.relevant is False
    assert decision.matched_themes == ()
    assert "dm" not in decision.matched_keywords


def test_crowdsourcing_theme_drops_userbot_release_updates_without_service_markers():
    text = (
        "#Update Moon Userbot has been updated to v3.0.0. "
        "Refactor help module, fix dispatcher, update custom modules."
    )

    decision = decide_text_relevance(
        text,
        include_themes=["众包任务"],
        min_keyword_hits=1,
    )

    assert decision.relevant is False
    assert decision.matched_themes == ()


def test_crowdsourcing_theme_keeps_real_group_pull_service_posts():
    text = (
        "指定群偷人 500人180元 TG手机号拉人 价格为0.5/人 "
        "欢迎老板砸单，联系客服 @Angelbabi520"
    )

    decision = decide_text_relevance(
        text,
        include_themes=["众包任务"],
        min_keyword_hits=1,
    )

    assert decision.relevant is True
    assert decision.matched_themes == ("众包任务",)
    assert "拉人" in decision.matched_keywords
