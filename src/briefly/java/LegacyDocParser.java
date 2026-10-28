// Local file parser entry point. Errors are isolated per input; no document text in logs.
import java.nio.file.*;
import java.nio.charset.StandardCharsets;

public class LegacyDocParser {
    public static void main(String[] args) throws Exception {
        Path root=Path.of(args[0]);
        Files.createDirectories(root);
        for(int i=1;i<args.length;i++) {
            Path output=root.resolve(Integer.toString(i-1));
            Files.createDirectories(output);
            try {
                LegacyDocStructured.main(new String[]{output.toString(),args[i]});
            } catch(LegacyDocStructured.UnsupportedStructure ex) {
                Files.writeString(output.resolve("error.code"),"unsupported_structure",StandardCharsets.UTF_8);
            } catch(Exception ex) {
                Files.writeString(output.resolve("error.code"),"parse_failed",StandardCharsets.UTF_8);
            }
        }
    }
}
