// Regression for Word controls found in authorized real DOC samples. No user content embedded.
public class WordCellProbe {
    static void check(String raw,String expected) throws Exception {
        String actual=LegacyDocStructured.cellText(raw);
        if(!expected.equals(actual))throw new AssertionError("Unexpected Word control normalization");
    }
    public static void main(String[] args) throws Exception {
        check("甲\u000b乙\r丙\u0007","甲 乙 丙");
        check("结果\u0015结束","结果结束");
        check("甲\u001e乙\u001f丙","甲\u2011乙\u00ad丙");
        check("合法\t文字\n😀","合法\t文字\n😀");
        boolean rejected=false;
        try{LegacyDocStructured.cellText("非法\u0000文本");}
        catch(LegacyDocStructured.UnsupportedStructure ex){rejected=true;}
        if(!rejected)throw new AssertionError("Unknown XML controls must not be silently deleted");
        System.out.println("5 Word control cases passed");
    }
}
